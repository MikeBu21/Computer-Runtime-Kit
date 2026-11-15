# -*- coding: utf-8 -*-
"""Manueller Steuerungsreset: Passwort, zwei Modi, kein Auto-Reset."""
from __future__ import annotations

import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from control_algorithm.consumption_history import get_weekday_profile, load_history
from control_algorithm.learning_persistence import (
    merge_learning_into_state,
    save_learning_store,
)
from control_algorithm.steuerungsreset import (
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


class TestSteuerungsreset(unittest.TestCase):
    def test_password_is_fixed_7777_and_not_in_the_page(self):
        self.assertEqual(RESET_PASSWORD, "7777")
        self.assertTrue(password_matches("7777"))
        self.assertFalse(password_matches("7778"))
        self.assertFalse(password_matches(""))
        html = Path(__file__).resolve().parents[1].joinpath("templates", "base.html").read_text(encoding="utf-8")
        self.assertNotIn("7777", html)
        self.assertIn("Steuerungsreset", html)
        self.assertIn('id="steuerungsreset_open"', html)
        self.assertIn('value=""', html)
        self.assertNotIn("threading", Path(__file__).resolve().parents[1].joinpath(
            "control_algorithm", "steuerungsreset.py"
        ).read_text(encoding="utf-8"))

    def test_confirm_and_typed_password_are_required(self):
        self.assertEqual(authorize_reset("7777", ""), "Bitte den Reset selbst bestätigen.")
        self.assertEqual(authorize_reset("", "1"), "Bitte das Passwort eintippen.")
        self.assertEqual(authorize_reset("7778", "1"), "Falsches Passwort.")
        self.assertIsNone(authorize_reset("7777", "1"))

    def test_last_mode_clears_profiles_keeps_archive_and_resets_ratio(self):
        with tempfile.TemporaryDirectory() as tmp:
            from control_algorithm.consumption_history import save_history

            save_history(tmp, _history())
            state = _state()
            result = apply_manual_reset(tmp, "last", state)
            history = load_history(tmp)
            self.assertEqual(len(history["days"]), 1)
            self.assertEqual(history["weekday_profiles"]["0"]["sample_count"], 0)
            self.assertEqual(history["profile_reset_from"], (date.today() + timedelta(days=1)).isoformat())
            prof = get_weekday_profile(history, 0, tmp)
            self.assertEqual(int(prof.get("sample_count") or 0), 0)
            self.assertEqual(state["performance_ratio"], 1.0)
            self.assertEqual(state["yield_factor"], 1.0)
            self.assertEqual(state["days_since_reset"], 0)
            self.assertEqual(state["hour_slot_factors"]["h08"], 1.2)
            self.assertEqual(state["steering_learning"]["params"]["soc_preempt_margin"], 7.5)
            self.assertEqual(len(state["yield_history"]), 1)
            self.assertIn("Archiv bleibt", result["message"])

    def test_archive_mode_also_deletes_raw_days(self):
        with tempfile.TemporaryDirectory() as tmp:
            from control_algorithm.consumption_history import save_history

            save_history(tmp, _history())
            state = _state()
            result = apply_manual_reset(tmp, "archiv", state)
            history = load_history(tmp)
            self.assertEqual(history["days"], [])
            self.assertEqual(result["archived_days"], 0)
            self.assertEqual(state["performance_ratio"], 1.0)
            self.assertIn("365-Tage-Archiv", result["message"])

    def test_saved_ratio_is_not_restored_from_the_learning_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "learning.json"
            save_learning_store(_state(), path)
            reset_state = _state()
            reset_state["performance_ratio"] = 1.0
            reset_state["yield_factor"] = 1.0
            reset_state["days_since_reset"] = 0
            save_learning_store(reset_state, path)
            restored = merge_learning_into_state(
                {
                    "performance_ratio": 1.0,
                    "yield_factor": 1.0,
                    "days_since_reset": 0,
                    "yield_history": reset_state["yield_history"],
                    "steering_learning": reset_state["steering_learning"],
                },
                path,
            )
            self.assertEqual(restored["days_since_reset"], 0)
            self.assertEqual(restored["performance_ratio"], 1.0)

    def test_first_evening_after_reset_does_not_freeze_ratio(self):
        from control_algorithm.calibration import run_evening_calibration
        from control_algorithm.slot_profiles import hour_slot_ids

        hours = {hid: 25.0 for hid in hour_slot_ids()}
        state = {
            "morning_pv_forecast_kwh": 900.0,
            "morning_forecast_slots": {},
            "morning_forecast_hours": {hid: 50.0 for hid in hour_slot_ids()},
            "morning_forecast_date": date.today().isoformat(),
            "performance_ratio": 1.0,
            "yield_factor": 1.0,
            "hour_slot_factors": {hid: 1.0 for hid in hour_slot_ids()},
            "days_since_reset": 0,
            "yield_history": [],
        }
        import control_algorithm.calibration as cal

        orig = cal.get_actual_pv_kwh_today
        orig_slots = cal.get_actual_pv_kwh_by_slots_today
        orig_hours = cal.get_actual_pv_kwh_by_hours_today
        try:
            cal.get_actual_pv_kwh_today = lambda _b: 450.0
            cal.get_actual_pv_kwh_by_slots_today = lambda _b: {}
            cal.get_actual_pv_kwh_by_hours_today = lambda _b: hours
            state = run_evening_calibration(state, {"pv_kwp": 16, "has_pv": True}, "/tmp")
        finally:
            cal.get_actual_pv_kwh_today = orig
            cal.get_actual_pv_kwh_by_slots_today = orig_slots
            cal.get_actual_pv_kwh_by_hours_today = orig_hours
        self.assertNotEqual(state["last_calibration_note"], "skipped_outlier")
        self.assertLess(state["performance_ratio"], 1.0)
        self.assertEqual(state["days_since_reset"], 1)

    def test_route_does_not_reset_without_typed_confirm(self):
        import app as dashboard

        client = dashboard.app.test_client()
        missing = client.post("/steuerungsreset", data={"password": "7777", "mode": "last"})
        self.assertEqual(missing.status_code, 400)
        wrong = client.post(
            "/steuerungsreset",
            data={"password": "0000", "confirm": "1", "mode": "last"},
        )
        self.assertEqual(wrong.status_code, 403)

        def fake_apply(base_dir, mode, state):
            state["performance_ratio"] = 1.0
            state["days_since_reset"] = 0
            return {"message": "test", "mode": mode}

        with patch("control_algorithm.config.load_state", return_value=_state()), patch(
            "control_algorithm.config.save_state"
        ) as save_state, patch(
            "control_algorithm.steuerungsreset.apply_manual_reset", side_effect=fake_apply
        ) as apply:
            ok = client.post(
                "/steuerungsreset",
                data={"password": "7777", "confirm": "1", "mode": "archiv"},
            )
        self.assertEqual(ok.status_code, 200)
        apply.assert_called_once()
        self.assertEqual(apply.call_args.args[1], "archiv")
        save_state.assert_called_once()
        self.assertEqual(save_state.call_args.args[0]["performance_ratio"], 1.0)


if __name__ == "__main__":
    unittest.main()
