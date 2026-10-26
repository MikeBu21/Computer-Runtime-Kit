"""Monatsspitze darf bei unsicheren Daten nicht gelöscht oder gesenkt werden."""
from __future__ import annotations

import json
from pathlib import Path

try:
    from energy_monthly_peak_15min import (  # noqa: F401
        _assess_peak_at,
        _load_state,
        _maybe_restore_bezug_peak,
        _merge_peak_candidate,
        _sanitize_stored_peak,
        _stats_path,
        _peak_backup_path,
    )
except ImportError:  # interne Peak-Helfer wurden umgebaut
    _assess_peak_at = None  # type: ignore


def _require_peak_api():
    if _assess_peak_at is None:
        return False
    return True


def test_sanitize_keeps_peak_when_timeseries_incomplete(tmp_path: Path):
    if not _require_peak_api():
        return
    from datetime import datetime as dt

    base = str(tmp_path)
    em = tmp_path / "energy_monitor"
    em.mkdir()
    today = dt.now().date().isoformat()
    peak_at = f"{today}T12:00:00"
    (em / "energy_timeseries_24h.json").write_text(
        json.dumps(
            {
                "date": today,
                "samples": [{"m": 12 * 60, "netz": 10.0}],  # incomplete 15-min window
            }
        ),
        encoding="utf-8",
    )
    state = {
        "month": today[:7],
        "peak_kw": 12.04,
        "peak_kwh": 3.01,
        "peak_at": peak_at,
    }
    _sanitize_stored_peak(base, state)
    assert state["peak_kw"] == 12.04
    assert _assess_peak_at(base, peak_at)["status"] == "incomplete"


def test_sanitize_does_not_downgrade_higher_rolling_peak(tmp_path: Path):
    if not _require_peak_api():
        return
    from datetime import datetime as dt

    base = str(tmp_path)
    em = tmp_path / "energy_monitor"
    em.mkdir()
    today = dt.now().date().isoformat()
    start = 12 * 60
    samples = [{"m": start + i, "netz": 4.0} for i in range(15)]
    (em / "energy_timeseries_24h.json").write_text(
        json.dumps({"date": today, "samples": samples}), encoding="utf-8"
    )
    state = {
        "month": today[:7],
        "peak_kw": 12.04,
        "peak_kwh": 3.01,
        "peak_at": f"{today}T12:00:00",
    }
    _sanitize_stored_peak(base, state)
    assert state["peak_kw"] == 12.04  # never reduce


def test_backup_other_day_is_kept_and_merged(tmp_path: Path):
    if not _require_peak_api():
        return
    from datetime import datetime as dt

    base = str(tmp_path)
    em = tmp_path / "energy_monitor"
    em.mkdir()
    month = dt.now().strftime("%Y-%m")
    # Main state has low peak from today
    main = {
        "month": month,
        "peak_kw": 2.0,
        "peak_kwh": 0.5,
        "peak_at": f"{dt.now().date().isoformat()}T10:00:00",
        "samples": [],
    }
    (_stats_path(base)).write_text(json.dumps(main), encoding="utf-8")
    # Backup has higher peak from earlier in the month
    backup = {
        "month": month,
        "peak_kw": 12.04,
        "peak_kwh": 3.01,
        "peak_at": f"{month}-05T14:00:00",
    }
    (_peak_backup_path(base)).write_text(json.dumps(backup), encoding="utf-8")
    loaded = _load_state(_stats_path(base), _peak_backup_path(base), month, base)
    assert float(loaded["peak_kw"]) == 12.04


def test_merge_peak_candidate_only_upward():
    if not _require_peak_api():
        return
    state = {"peak_kw": 5.0, "peak_at": "x", "peak_kwh": 1.25}
    assert _merge_peak_candidate(state, 4.0, "y") is False
    assert state["peak_kw"] == 5.0
    assert _merge_peak_candidate(state, 6.0, "z") is True
    assert state["peak_kw"] == 6.0


def test_timeseries_upgrades_but_never_lowers(tmp_path: Path, monkeypatch):
    if not _require_peak_api():
        return
    from datetime import datetime as dt

    base = str(tmp_path)
    em = tmp_path / "energy_monitor"
    em.mkdir()
    today = dt.now().date().isoformat()
    start = 15 * 60 + 24
    samples = [{"m": start + i, "netz": 20.0} for i in range(15)]
    (em / "energy_timeseries_24h.json").write_text(
        json.dumps({"date": today, "samples": samples}), encoding="utf-8"
    )
    state = {
        "month": today[:7],
        "peak_kw": 3.01,
        "peak_kwh": 0.75,
        "peak_at": f"{today}T08:00:00",
        "samples": [],
    }
    _maybe_restore_bezug_peak(base, state)
    assert float(state["peak_kw"]) == 20.0
    # Lower timeseries must not pull peak down
    samples_low = [{"m": start + i, "netz": 1.0} for i in range(15)]
    (em / "energy_timeseries_24h.json").write_text(
        json.dumps({"date": today, "samples": samples_low}), encoding="utf-8"
    )
    _maybe_restore_bezug_peak(base, state)
    assert float(state["peak_kw"]) == 20.0
