# -*- coding: utf-8 -*-
"""Monatsspitze: höchste volle Viertelstunde Netzbezug."""
import json
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from billing_month_peak import (
    billing_month_peak,
    quarter_import_kw,
    quarters_from_samples,
)


def _minutes(start, values):
    return [{"m": start + offset, "netz": value} for offset, value in enumerate(values)]


class TestBillingMonthPeak(unittest.TestCase):
    def test_full_quarter_is_the_mean_import(self):
        minutes = {offset: Decimal("6") for offset in range(10)}
        minutes.update({offset: Decimal("-2") for offset in range(10, 15)})
        self.assertEqual(quarter_import_kw(minutes), Decimal("4"))

    def test_missing_minute_is_not_a_billing_interval(self):
        minutes = {offset: Decimal("8") for offset in range(14)}
        self.assertIsNone(quarter_import_kw(minutes))

    def test_quarters_follow_the_clock(self):
        samples = _minutes(0, [1] * 15) + _minutes(15, [9] * 15)
        found = dict(quarters_from_samples(samples))
        self.assertEqual(found[0], Decimal("1"))
        self.assertEqual(found[1], Decimal("9"))
        self.assertNotIn(2, found)

    def test_month_peak_is_the_highest_complete_quarter(self):
        day = date.today()
        if day.day == 1:
            day = day.replace(day=2)
        with TemporaryDirectory() as raw:
            root = Path(raw)
            folder = root / "energy_monitor" / "energy_timeseries_days"
            folder.mkdir(parents=True)
            earlier = day.replace(day=1).isoformat()
            (folder / f"{earlier}.json").write_text(
                json.dumps({"date": earlier, "samples": _minutes(0, [2] * 15)}),
                encoding="utf-8",
            )
            live = root / "energy_monitor" / "energy_timeseries_24h.json"
            live.write_text(
                json.dumps({"date": day.isoformat(), "samples": _minutes(45, [9] * 14)}),
                encoding="utf-8",
            )
            peak = billing_month_peak(str(root), today=day)
        self.assertEqual(peak["peak_kw"], 2.0)
        self.assertTrue(peak["peak_at"].startswith(earlier))
        self.assertEqual(peak["quarters"], 1)


if __name__ == "__main__":
    unittest.main()
