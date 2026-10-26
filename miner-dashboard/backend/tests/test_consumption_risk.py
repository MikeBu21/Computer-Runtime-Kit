# -*- coding: utf-8 -*-
"""Verbrauchsrisiko: 0% = historisch, 100% = doppelt."""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from control_algorithm.energy_balance import (
    consumption_risk_scale,
    normalize_consumption_risk_percent,
    planned_base_consumption_kwh,
)


class TestConsumptionRisk(unittest.TestCase):
    def test_normalize_bounds(self):
        self.assertEqual(normalize_consumption_risk_percent(None), 0.0)
        self.assertEqual(normalize_consumption_risk_percent(-5), 0.0)
        self.assertEqual(normalize_consumption_risk_percent(100), 100.0)
        self.assertEqual(normalize_consumption_risk_percent(1000), 1000.0)
        self.assertEqual(normalize_consumption_risk_percent(1500), 1000.0)

    def test_scale_mapping(self):
        self.assertAlmostEqual(consumption_risk_scale({"consumption_risk_percent": 0}), 1.0)
        self.assertAlmostEqual(consumption_risk_scale({"consumption_risk_percent": 100}), 2.0)
        self.assertAlmostEqual(consumption_risk_scale({"consumption_risk_percent": 1000}), 11.0)
        self.assertAlmostEqual(consumption_risk_scale({"consumption_risk_percent": 50}), 1.5)

    def test_planned_base_scales_historical(self):
        start = datetime(2026, 8, 6, 12, 0, 0)
        end = start + timedelta(hours=2)
        with patch(
            "control_algorithm.energy_balance.expected_base_consumption_kwh",
            return_value=10.0,
        ):
            zero = planned_base_consumption_kwh(
                {"consumption_risk_percent": 0}, "/tmp", start, end
            )
            double = planned_base_consumption_kwh(
                {"consumption_risk_percent": 100}, "/tmp", start, end
            )
            maxed = planned_base_consumption_kwh(
                {"consumption_risk_percent": 1000}, "/tmp", start, end
            )
        self.assertAlmostEqual(zero, 10.0)
        self.assertAlmostEqual(double, 20.0)
        self.assertAlmostEqual(maxed, 110.0)


if __name__ == "__main__":
    unittest.main()
