# -*- coding: utf-8 -*-
"""Lerndaten dürfen bei Config-Änderung nicht verloren gehen."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from control_algorithm.calibration import maybe_reset_on_config_change
from control_algorithm.config import config_fingerprint
from control_algorithm.learning_persistence import (
    load_learning_store,
    merge_learning_into_state,
    save_learning_store,
)
from control_algorithm.steering_learning import (
    ensure_steering_learning,
    reset_steering_learning,
)


class TestLearningNeverWiped(unittest.TestCase):
    def test_config_change_preserves_steering_learning(self):
        config_a = {
            "has_pv": True,
            "pv_kwp": 16,
            "perfect_weather_sun_hours": 6,
            "city": "Beispiel",
            "has_storage": True,
            "storage_kwh": 40,
        }
        config_b = dict(config_a)
        config_b["storage_kwh"] = 45  # Fingerprint ändert sich
        state = {
            "config_fingerprint": config_fingerprint(config_a),
            "performance_ratio": 0.92,
            "yield_history": [{"date": "2026-09-01", "actual": 50}],
            "days_since_reset": 12,
            "accuracy_score": 70.0,
            "steering_learning": {
                "params": {"soc_preempt_margin": 7.5, "score": 60.0},
                "history": [{"date": "2026-09-20", "miss_export_kwh": 3.0}],
                "today": {"date": "2026-09-23"},
                "last_run": "2026-09-22T23:00:00",
            },
            "slot_factors": {"morning": 1.2, "midday": 1.0, "afternoon": 0.9, "evening": 0.8},
        }
        state, _ = maybe_reset_on_config_change(state, config_b)
        self.assertEqual(state["config_fingerprint"], config_fingerprint(config_b))
        self.assertEqual(state["performance_ratio"], 0.92)
        self.assertEqual(len(state["yield_history"]), 1)
        self.assertEqual(state["days_since_reset"], 12)
        self.assertEqual(state["steering_learning"]["params"]["soc_preempt_margin"], 7.5)
        self.assertEqual(len(state["steering_learning"]["history"]), 1)
        self.assertTrue(state.get("learning_preserved_on_config_change"))

    def test_reset_without_force_is_noop(self):
        state: dict = {}
        ensure_steering_learning(state)
        state["steering_learning"]["params"]["soc_preempt_margin"] = 8.0
        state["steering_learning"]["history"] = [{"date": "2026-09-01"}]
        reset_steering_learning(state)  # ohne force
        self.assertEqual(state["steering_learning"]["params"]["soc_preempt_margin"], 8.0)
        self.assertEqual(len(state["steering_learning"]["history"]), 1)

    def test_sidecar_restores_empty_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "learning_store.json"
            rich = {
                "steering_learning": {
                    "params": {"soc_preempt_margin": 6.0},
                    "history": [{"date": "2026-09-10"}, {"date": "2026-09-11"}],
                    "last_run": "2026-09-11T23:00:00",
                },
                "yield_history": [{"date": "2026-09-10", "actual": 40}],
                "days_since_reset": 5,
            }
            # save via helper needs state shape
            save_learning_store(rich, path)
            wiped = {
                "steering_learning": {
                    "params": {"soc_preempt_margin": 6.0},
                    "history": [],
                    "last_run": None,
                },
                "yield_history": [],
                "days_since_reset": 0,
            }
            restored = merge_learning_into_state(wiped, path)
            self.assertEqual(len(restored["steering_learning"]["history"]), 2)
            self.assertEqual(restored["steering_learning"]["last_run"], "2026-09-11T23:00:00")
            self.assertEqual(len(restored["yield_history"]), 1)
            self.assertEqual(restored["days_since_reset"], 5)
            store = load_learning_store(path)
            self.assertIn("steering_learning", store)


if __name__ == "__main__":
    unittest.main()
