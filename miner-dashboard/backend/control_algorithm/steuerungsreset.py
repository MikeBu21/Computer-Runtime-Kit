# -*- coding: utf-8 -*-
"""Manueller Steuerungsreset.

Läuft ausschließlich, wenn der Nutzer das Passwort selbst eintippt und
danach selbst bestätigt. Kein Timer, kein Start beim Import, kein stilles
Zurücksetzen im Hintergrund.
"""
from __future__ import annotations

import hmac
from datetime import date, datetime, timedelta
from typing import Any, Dict, Optional

# Festes Passwort. Nicht konfigurierbar. Vier mal die 7.
RESET_PASSWORD = "7777"

MODE_LAST = "last"
MODE_ARCHIV = "archiv"


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


def apply_manual_reset(
    base_dir: str,
    mode: str,
    state: Dict[str, Any],
) -> Dict[str, Any]:
    """Setzt Profile und PV-Ratio zurück. Nur nach authorize_reset aufrufen."""
    if mode not in (MODE_LAST, MODE_ARCHIV):
        raise ValueError("Unbekannter Modus.")

    from control_algorithm.consumption_history import (
        _empty_weekday_profile,
        load_history,
        save_history,
    )

    history = load_history(base_dir)
    if mode == MODE_ARCHIV:
        history["days"] = []
    history["weekday_profiles"] = {
        str(w): _empty_weekday_profile() for w in range(7)
    }
    # Bestehende Archivtage bleiben bei Modus „nur Last“ liegen, zählen aber
    # erst ab morgen wieder ins Profil. Sonst baut der nächste Lesezugriff
    # das gelöschte Lastprofil sofort aus dem alten Archiv neu.
    history["profile_reset_from"] = (date.today() + timedelta(days=1)).isoformat()
    save_history(base_dir, history)

    state["performance_ratio"] = 1.0
    state["yield_factor"] = 1.0
    # Tag 0: die Abendkalibrierung darf die Ratio anpassen und friert sie
    # bei einem 100-%-Tag nicht ein (Schwelle gilt erst ab dem Folgetag).
    state["days_since_reset"] = 0
    state["last_manual_reset_at"] = datetime.now().isoformat(timespec="seconds")
    state["last_manual_reset_mode"] = mode

    if mode == MODE_ARCHIV:
        message = (
            "Lastprofil, Wochentagsprofil und das 365-Tage-Archiv sind gelöscht. "
            "Die PV-Ratio steht auf 1. Das Neulernen läuft ab dem nächsten Tag."
        )
    else:
        message = (
            "Lastprofil und Wochentagsprofil sind gelöscht. Das Archiv bleibt. "
            "Die PV-Ratio steht auf 1. Das Neulernen läuft ab dem nächsten Tag."
        )
    return {
        "ok": True,
        "mode": mode,
        "message": message,
        "archived_days": len(history.get("days") or []),
        "performance_ratio": 1.0,
        "days_since_reset": 0,
    }
