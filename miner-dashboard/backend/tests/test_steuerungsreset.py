# -*- coding: utf-8 -*-
"""Manueller Steuerungsreset: Passwort, zwei Punkte, kein Auto-Reset."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from control_algorithm.consumption_history import load_history, save_history
from control_algorithm.steuerungsreset import (
    MODE_ARCHIV,
    MODE_SONST,
    RESET_PASSWORD,
    apply_manual_reset,
    authorize_reset,
    password_matches,
)


def _history():
    hourly = [1.5] * 24
    return {
        "days": [
            {
                "date": "2026-10-05",
                "weekday": 0,
                "hourly_base_kwh": hourly,
                "hourly_hydro_kwh": [0.2] * 24,
                "hourly_verbrauch_kwh": hourly,
                "hourly_verbrauch_pct": [round(1 / 24, 6)] * 24,
                "hourly_base_pct": [round(1 / 24, 6)] * 24,
                "total_base_kwh": 36.0,
                "total_verbrauch_kwh": 36.0,
            }
        ],
        "weekday_profiles": {
            "0": {
                "hourly_base_kwh_avg": [2.0] * 24,
                "hourly_hydro_kwh_avg": [0.2] * 24,
                "hourly_verbrauch_pct_avg": [round(1 / 24, 6)] * 24,
                "hourly_base_pct_avg": [round(1 / 24, 6)] * 24,
                "sample_count": 4,
            }
        },
    }


def _state():
    return {
        "performance_ratio": 1.0774,
        "yield_factor": 1.0774,
        "days_since_reset": 11,
        "hour_slot_factors": {"h08": 1.2},
        "steering_learning": {"params": {"soc_preempt_margin": 7.5}, "history": [{"date": "2026-10-01"}]},
        "yield_history": [{"date": "2026-10-01", "actual": 40}],
    }


def _seed_sonst(tmp: str) -> None:
    monitor = Path(tmp) / "energy_monitor"
    monitor.mkdir(parents=True, exist_ok=True)
    for name in ("energy_stats_24h.json", "energy_stats_30d.json", "energy_stats_year.json"):
        (monitor / name).write_text(
            json.dumps({"marker": name, "verbrauch_gesamt_kwh": 42.0}),
            encoding="utf-8",
        )
    (monitor / "grundverbrauch_rechner.json").write_text(
        json.dumps(
            {
                "assumptions": {"miner:1": "on"},
                "recommendation": {"kw": 3.5},
                "base_archive": [{"date": "2026-10-01"}],
                "targets": {"soc_hold_enabled": True, "soc_hold_percent": 40.0},
                "forward_purchase": {"active": True},
            }
        ),
        encoding="utf-8",
    )


class TestSteuerungsreset(unittest.TestCase):
    def setUp(self):
        self.bms_save = patch("bms_seite.save_settings", return_value={}).start()
        self.addCleanup(patch.stopall)

    def test_password_is_fixed_7777_and_not_in_the_page(self):
        self.assertEqual(RESET_PASSWORD, "7777")
        self.assertTrue(password_matches("7777"))
        self.assertFalse(password_matches("7778"))
        self.assertFalse(password_matches(""))
        html = Path(__file__).resolve().parents[1].joinpath("templates", "base.html").read_text(encoding="utf-8")
        self.assertNotIn("7777", html)
        self.assertIn("Steuerungsreset", html)
        self.assertIn('id="steuerungsreset_open"', html)
        self.assertIn('value="archiv"', html)
        self.assertIn('value="sonst_alles"', html)
        self.assertIn("steuerungsreset_gvr_warn", html)
        self.assertNotIn('value="last"', html)
        self.assertIn('value=""', html)
        self.assertNotIn("threading", Path(__file__).resolve().parents[1].joinpath(
            "control_algorithm", "steuerungsreset.py"
        ).read_text(encoding="utf-8"))

    def test_confirm_and_typed_password_are_required(self):
        self.assertEqual(authorize_reset("7777", ""), "Bitte den Reset selbst bestätigen.")
        self.assertEqual(authorize_reset("", "1"), "Bitte das Passwort eintippen.")
        self.assertEqual(authorize_reset("7778", "1"), "Falsches Passwort.")
        self.assertIsNone(authorize_reset("7777", "1"))

    def test_archiv_mode_clears_only_archive_days(self):
        with tempfile.TemporaryDirectory() as tmp:
            save_history(tmp, _history())
            state = _state()
            result = apply_manual_reset(tmp, MODE_ARCHIV, state)
            history = load_history(tmp)
            self.assertEqual(history["days"], [])
            self.assertEqual(history["weekday_profiles"]["0"]["sample_count"], 4)
            self.assertEqual(result["archived_days"], 0)
            self.assertEqual(state["performance_ratio"], 1.0774)
            self.assertEqual(state["yield_factor"], 1.0774)
            self.assertEqual(state["days_since_reset"], 11)
            self.assertEqual(state["hour_slot_factors"]["h08"], 1.2)
            self.assertEqual(state["steering_learning"]["params"]["soc_preempt_margin"], 7.5)
            self.assertEqual(state["last_manual_reset_mode"], MODE_ARCHIV)
            self.assertIn("365-Tage-Verbrauchsarchiv", result["message"])
            self.bms_save.assert_not_called()

    def test_sonst_alles_clears_energy_stats_and_gvr_keeps_learning(self):
        with tempfile.TemporaryDirectory() as tmp:
            save_history(tmp, _history())
            _seed_sonst(tmp)
            state = _state()
            result = apply_manual_reset(tmp, MODE_SONST, state)
            monitor = Path(tmp) / "energy_monitor"
            for name in ("energy_stats_24h.json", "energy_stats_30d.json", "energy_stats_year.json"):
                data = json.loads((monitor / name).read_text(encoding="utf-8"))
                self.assertNotIn("marker", data)
                self.assertEqual(float(data.get("verbrauch_gesamt_kwh") or 0), 0.0)
            gvr = json.loads((monitor / "grundverbrauch_rechner.json").read_text(encoding="utf-8"))
            self.assertEqual(gvr.get("assumptions"), {})
            self.assertEqual(gvr.get("recommendation"), {})
            self.assertEqual(gvr.get("base_archive"), [])
            self.assertFalse((gvr.get("targets") or {}).get("soc_hold_enabled"))
            history = load_history(tmp)
            self.assertEqual(len(history["days"]), 1)
            self.assertEqual(state["performance_ratio"], 1.0774)
            self.assertEqual(state["hour_slot_factors"]["h08"], 1.2)
            self.assertEqual(state["steering_learning"]["params"]["soc_preempt_margin"], 7.5)
            self.assertEqual(state["last_manual_reset_mode"], MODE_SONST)
            self.assertIn("grundverbrauch_rechner.json", result["cleared"])
            self.assertIn("Grundverbrauchsrechner", result["message"])
            self.bms_save.assert_called_once_with(
                {
                    "manual_off": True,
                    "real_mode": False,
                    "planning_hold": False,
                    "community_hold": False,
                    "exchange_hold": False,
                }
            )

    def test_unknown_mode_is_rejected(self):
        with self.assertRaises(ValueError):
            apply_manual_reset("/tmp", "last", _state())

    def test_route_does_not_reset_without_typed_confirm(self):
        import app as dashboard

        client = dashboard.app.test_client()
        missing = client.post("/steuerungsreset", data={"password": "7777", "mode": "archiv"})
        self.assertEqual(missing.status_code, 400)
        wrong = client.post(
            "/steuerungsreset",
            data={"password": "0000", "confirm": "1", "mode": "archiv"},
        )
        self.assertEqual(wrong.status_code, 403)

        def fake_apply(base_dir, mode, state):
            state["last_manual_reset_mode"] = mode
            return {"message": "test", "mode": mode}

        with patch("control_algorithm.config.load_state", return_value=_state()), patch(
            "control_algorithm.config.save_state"
        ) as save_state, patch(
            "control_algorithm.steuerungsreset.apply_manual_reset", side_effect=fake_apply
        ) as apply:
            ok = client.post(
                "/steuerungsreset",
                data={"password": "7777", "confirm": "1", "mode": "sonst_alles"},
            )
        self.assertEqual(ok.status_code, 200)
        apply.assert_called_once()
        self.assertEqual(apply.call_args.args[1], "sonst_alles")
        save_state.assert_called_once()
        self.assertEqual(save_state.call_args.args[0]["last_manual_reset_mode"], "sonst_alles")


if __name__ == "__main__":
    unittest.main()
