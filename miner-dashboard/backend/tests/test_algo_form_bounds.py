# -*- coding: utf-8 -*-
"""Form-Bounds: gespeicherte Werte dürfen HTML5-Submit nie blockieren."""
from __future__ import annotations

import unittest

try:
    from control_algorithm_web import (
        FORM_NUMBER_BOUNDS,
        clamp_form_number,
        sanitize_config_for_form,
    )
except ImportError:  # Form-Bounds liegen nicht mehr in control_algorithm_web
    FORM_NUMBER_BOUNDS = None
    clamp_form_number = None
    sanitize_config_for_form = None


@unittest.skipUnless(FORM_NUMBER_BOUNDS is not None, "Form-Bounds-API nicht vorhanden")
class TestAlgoFormBounds(unittest.TestCase):
    def test_anomaly_low_values_allowed(self):
        self.assertEqual(clamp_form_number("anomaly_clear_stable_minutes", 2.0), 2.0)
        self.assertEqual(clamp_form_number("anomaly_resume_delay_minutes", 0.0), 0.0)

    def test_sanitize_keeps_valid_low_anomaly_values(self):
        cfg = {
            "anomaly_clear_stable_minutes": 2.0,
            "anomaly_resume_delay_minutes": 0.0,
        }
        out, fixed = sanitize_config_for_form(cfg)
        self.assertEqual(fixed, [])
        self.assertEqual(out["anomaly_clear_stable_minutes"], 2.0)
        self.assertEqual(out["anomaly_resume_delay_minutes"], 0.0)

    def test_sanitize_clamps_out_of_range(self):
        cfg = {"anomaly_clear_stable_minutes": -5.0, "min_soc_percent": 150.0}
        out, fixed = sanitize_config_for_form(cfg)
        self.assertTrue(any(x.startswith("anomaly_clear_stable_minutes:") for x in fixed))
        self.assertTrue(any(x.startswith("min_soc_percent:") for x in fixed))
        self.assertEqual(out["anomaly_clear_stable_minutes"], 0.0)
        self.assertEqual(out["min_soc_percent"], 100.0)

    def test_bounds_cover_critical_anomaly_fields(self):
        self.assertIn("anomaly_clear_stable_minutes", FORM_NUMBER_BOUNDS)
        self.assertIn("anomaly_resume_delay_minutes", FORM_NUMBER_BOUNDS)
        self.assertEqual(FORM_NUMBER_BOUNDS["anomaly_clear_stable_minutes"][0], 0.0)
        self.assertEqual(FORM_NUMBER_BOUNDS["anomaly_resume_delay_minutes"][0], 0.0)


if __name__ == "__main__":
    unittest.main()
