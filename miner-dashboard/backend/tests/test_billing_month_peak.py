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
    hypothetical_month_peak,
    load_peak_entry,
    peak_money,
    quarter_import_kw,
    quarters_from_samples,
    save_peak_entry,
    uncovered_import_kw,
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

    def test_uncovered_import_is_load_minus_generation(self):
        self.assertEqual(
            uncovered_import_kw({"betrieb": "8", "erzeugung": "3", "netz": "1"}),
            Decimal("5"),
        )
        self.assertEqual(uncovered_import_kw({"betrieb": 1, "erzeugung": 4}), Decimal("0"))
        self.assertIsNone(uncovered_import_kw({"netz": 6}))

    def test_hypothetical_peak_ignores_the_shaved_grid_value(self):
        day = date.today()
        if day.day == 1:
            day = day.replace(day=2)
        samples = [
            {"m": offset, "netz": 1, "betrieb": 9, "erzeugung": 1}
            for offset in range(15)
        ]
        with TemporaryDirectory() as raw:
            root = Path(raw)
            folder = root / "energy_monitor" / "energy_timeseries_days"
            folder.mkdir(parents=True)
            earlier = day.replace(day=1).isoformat()
            (folder / f"{earlier}.json").write_text(
                json.dumps({"date": earlier, "samples": samples}),
                encoding="utf-8",
            )
            measured = billing_month_peak(str(root), today=day)
            hypo = hypothetical_month_peak(str(root), today=day)
        self.assertEqual(measured["peak_kw"], 1.0)
        self.assertEqual(hypo["peak_kw"], 8.0)

    def test_tariff_costs_and_saved_amount(self):
        money = peak_money(2.89, 6.0, 4.85)
        self.assertEqual(money["cost_eur"], round(2.89 * 4.85, 2))
        self.assertEqual(money["hypothetical_cost_eur"], round(6.0 * 4.85, 2))
        self.assertEqual(money["saved_eur"], round((6.0 - 2.89) * 4.85, 2))
        self.assertIsNone(peak_money(2.89, 6.0, None)["saved_eur"])

    def test_entry_roundtrip_clears_empty_values(self):
        with TemporaryDirectory() as raw:
            root = Path(raw) / "backend"
            (root / "energy_monitor").mkdir(parents=True)
            saved = save_peak_entry(str(root), {
                "peak_kw": "2,89",
                "hypothetical_kw": 6,
                "tariff_eur_per_kw": 4.85,
            })
            self.assertEqual(saved["peak_kw"], 2.89)
            self.assertEqual(load_peak_entry(str(root))["tariff_eur_per_kw"], 4.85)
            cleared = save_peak_entry(str(root), {"peak_kw": ""})
            self.assertIsNone(cleared["peak_kw"])
            self.assertEqual(cleared["hypothetical_kw"], 6.0)


if __name__ == "__main__":
    unittest.main()
