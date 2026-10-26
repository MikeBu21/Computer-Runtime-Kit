# -*- coding: utf-8 -*-
"""Lokale Pfade für Energiedaten (Pi vs. GitHub-Layouts)."""
from __future__ import annotations

from pathlib import Path
from typing import Optional, Union

PathLike = Union[str, Path]

# Marker-Dateien, die den echten Energy-Ordner erkennen
_ENERGY_MARKERS = (
    "energy_stats_24h.json",
    "energy_timeseries_24h.json",
    "energy_value.txt",
    "consumption_history.json",
)


def energy_data_dir(base_dir: Optional[PathLike] = None) -> Path:
    """
    Ermittelt den Energy-Datenordner.

    Auf diesem Pi liegen die Live-Dateien unter ``backend/energy_monitor/``.
    Manche GitHub-Stände legen Stats direkt unter ``backend/`` oder ``backend/energy/``.
    Es wird der Ordner gewählt, der vorhandene Marker-Dateien enthält.
    """
    from control_algorithm.config import BACKEND_DIR

    base = Path(base_dir) if base_dir else BACKEND_DIR
    candidates = [
        base / "energy_monitor",
        base / "energy",
        base,
    ]
    for cand in candidates:
        if not cand.is_dir():
            continue
        for marker in _ENERGY_MARKERS:
            if (cand / marker).is_file():
                return cand
    # Lokaler Standard auf dem Pi
    return base / "energy_monitor"


def energy_file(base_dir: Optional[PathLike], *parts: str) -> Path:
    """Pfad zu einer Datei im aufgelösten Energy-Ordner."""
    return energy_data_dir(base_dir).joinpath(*parts)
