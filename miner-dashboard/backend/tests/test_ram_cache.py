# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import os
from pathlib import Path

import ram_cache


def test_live_read_write_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("MINER_RAM_CACHE", "1")
    monkeypatch.setenv("MINER_RAM_ROOT", str(tmp_path / "ram"))
    base = tmp_path / "backend"
    (base / "energy_monitor").mkdir(parents=True)

    ram_cache.write_text(base, "pv_value.txt", "12.5", scope="energy_monitor")
    assert ram_cache.read_text(base, "pv_value.txt", scope="energy_monitor") == "12.5"

    payload = {"values_kw": {"pv": 3.0}}
    ram_cache.write_json(base, "enline_values.json", payload, scope="energy_monitor")
    loaded = ram_cache.read_json(base, "enline_values.json", scope="energy_monitor")
    assert loaded["values_kw"]["pv"] == 3.0


def test_timeseries_flush_to_sd(tmp_path, monkeypatch):
    monkeypatch.setenv("MINER_RAM_CACHE", "1")
    monkeypatch.setenv("MINER_RAM_ROOT", str(tmp_path / "ram"))
    base = tmp_path / "backend"
    (base / "energy_monitor").mkdir(parents=True)
    ts = {"date": "2026-08-30", "samples": [{"m": 0, "betrieb": 1.0}]}
    ram_path = ram_cache.timeseries_path(base, "energy_timeseries_24h.json")
    ram_path.parent.mkdir(parents=True, exist_ok=True)
    ram_path.write_text(json.dumps(ts), encoding="utf-8")

    ram_cache.flush_timeseries_to_sd(base)
    sd_path = base / "energy_monitor" / "energy_timeseries_24h.json"
    assert sd_path.is_file()
    assert json.loads(sd_path.read_text())["samples"][0]["betrieb"] == 1.0

    stats = {"date": "2026-08-30", "verbrauch_gesamt_kwh": 42.0}
    stats_ram = ram_cache.live_energy_path(base, "energy_stats_24h.json")
    stats_ram.write_text(json.dumps(stats), encoding="utf-8")
    ram_cache.write_text(base, "wohnhaus_value.txt", "0.55", scope="energy_monitor")
    ram_cache.flush_timeseries_to_sd(base)
    sd_stats = base / "energy_monitor" / "energy_stats_24h.json"
    assert sd_stats.is_file()
    assert json.loads(sd_stats.read_text())["verbrauch_gesamt_kwh"] == 42.0
    assert (base / "energy_monitor" / "wohnhaus_value.txt").read_text() == "0.55"


def test_stale_ram_loses_to_newer_sd_copy(tmp_path, monkeypatch):
    import os
    import time

    monkeypatch.setenv("MINER_RAM_CACHE", "1")
    monkeypatch.setenv("MINER_RAM_ROOT", str(tmp_path / "ram"))
    base = tmp_path / "backend"
    em = base / "energy_monitor"
    em.mkdir(parents=True)
    ram_cache.write_text(base, "pv_value.txt", "5.727", scope="energy_monitor")
    ram_path = ram_cache.live_energy_path(base, "pv_value.txt")
    old = time.time() - 3600
    os.utime(ram_path, (old, old))
    (em / "pv_value.txt").write_text("7.2", encoding="utf-8")
    assert ram_cache.read_text(base, "pv_value.txt", scope="energy_monitor") == "7.2"


def test_hydrate_restores_live_energy_files(tmp_path, monkeypatch):
    monkeypatch.setenv("MINER_RAM_CACHE", "1")
    monkeypatch.setenv("MINER_RAM_ROOT", str(tmp_path / "ram"))
    base = tmp_path / "backend"
    em = base / "energy_monitor"
    em.mkdir(parents=True)
    (em / "wohnhaus_value.txt").write_text("0.42", encoding="utf-8")
    (em / "pv_value.txt").write_text("1.1", encoding="utf-8")

    ram_cache.hydrate_from_sd(str(base))
    assert ram_cache.read_text(base, "wohnhaus_value.txt", scope="energy_monitor") == "0.42"
    assert ram_cache.read_text(base, "pv_value.txt", scope="energy_monitor") == "1.1"


def test_ephemeral_backend_path(tmp_path, monkeypatch):
    monkeypatch.setenv("MINER_RAM_CACHE", "1")
    monkeypatch.setenv("MINER_RAM_ROOT", str(tmp_path / "ram"))
    base = tmp_path / "backend"
    path = ram_cache.backend_ephemeral_path(base, "control_algorithm_heartbeat.json")
    assert str(tmp_path / "ram" / "backend" / "control_algorithm_heartbeat.json") == str(path)


def test_monthly_peak_ram_state_sd_backup(tmp_path, monkeypatch):
    monkeypatch.setenv("MINER_RAM_CACHE", "1")
    monkeypatch.setenv("MINER_RAM_ROOT", str(tmp_path / "ram"))
    base = str(tmp_path / "backend")
    (tmp_path / "backend" / "energy_monitor").mkdir(parents=True)

    from energy_monthly_peak_15min import (
        _peak_backup_path,
        _save_peak_backup_durable,
        _save_state_ram,
        _stats_path,
    )

    state_path = _stats_path(base)
    backup_path = _peak_backup_path(base)
    assert "ram" in str(state_path)
    assert "peak.json" in str(backup_path)
    assert not str(backup_path).startswith(str(tmp_path / "ram"))

    _save_state_ram(state_path, {"month": "2026-08", "samples": [{"t": "x", "kw": 1.0}]})
    assert state_path.is_file()
    assert not (tmp_path / "backend" / "energy_monitor" / "monthly_peak_15min.json").is_file()

    _save_peak_backup_durable(
        backup_path,
        {"month": "2026-08", "peak_kw": 9.5, "peak_kwh": 2.38, "peak_at": "2026-08-30T12:00:00"},
    )
    assert backup_path.is_file()
    saved = json.loads(backup_path.read_text())
    assert saved["peak_kw"] == 9.5
