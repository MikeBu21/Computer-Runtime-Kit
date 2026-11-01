# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest

import bms_seite
from bms_seite import (
    ENERGY_PATTERN_LOAD_FIRST,
    MAX_DISCHARGE_AMPS,
    TOU_HOLD_POWER_W,
    TOU_HOLD_SOC,
    TOU_WEEK_ENABLED,
    WORK_MODE_EXPORT_FIRST,
    WORK_MODE_ZERO_EXPORT_CT,
    ZERO_EXPORT_IDLE_W,
    _charge_writes,
    _control_item,
    _hold_deadband_w,
    _needed_charge_amps,
    _peak_enable_value,
    _release_writes,
    _tou_hold_writes,
    _tou_soc_for_hold,
)


def _item():
    return _control_item(
        bms_seite.INVERTERS["deye_hybrid"],
        {"lookup": "sg04lp3", "model": "SUN-12K-SG04LP3"},
    )


def _by_addr(rows):
    return {int(addr): int(value) for addr, _name, value in rows}


class HoldGridWritesTests(unittest.TestCase):
    def setUp(self):
        bms_seite._commanded_amps = 0

    def test_three_phase_has_peak_and_tou(self):
        item = _item()
        self.assertEqual(item["register"], 0x0068)
        self.assertEqual(item["time_of_use_register"], 0x0092)
        self.assertEqual(item["grid_peak_power_register"], 0x00BF)

    def test_tou_stays_enabled_when_grid_is_high(self):
        rows = _charge_writes(
            _item(),
            2.0,
            {"grid_kw": 8.7, "battery_kw": -0.2, "soc_percent": 86.0},
            peak_flags=0,
        )
        values = _by_addr(rows)
        self.assertEqual(values[0x0092], TOU_WEEK_ENABLED)
        self.assertNotEqual(values[0x0092], 0)
        self.assertEqual(values[0x008E], WORK_MODE_ZERO_EXPORT_CT)
        self.assertEqual(values[0x008D], ENERGY_PATTERN_LOAD_FIRST)
        self.assertEqual(values[0x0091], 0)
        self.assertEqual(values[0x00BF], 2000)
        self.assertEqual(values[0x00B2] & 16, 16)
        self.assertEqual(values[0x0080], 0)
        self.assertEqual(values[0x0082], 0)
        self.assertEqual(values[0x006D], MAX_DISCHARGE_AMPS)
        self.assertEqual(values[0x00E7], 2)
        self.assertEqual(values[0x00E9], 200)

    def test_grid_over_target_discharges_even_if_battery_looks_like_charging(self):
        rows = _charge_writes(
            _item(),
            2.0,
            {"grid_kw": 2.8, "battery_kw": 0.9, "soc_percent": 85.0, "load_kw": 2.9},
            peak_flags=16,
        )
        values = _by_addr(rows)
        self.assertEqual(values[0x006D], MAX_DISCHARGE_AMPS)
        self.assertEqual(values[0x0080], 0)
        self.assertEqual(values[0x0082], 0)

    def test_hold_deadband_scales_for_low_targets(self):
        self.assertEqual(_hold_deadband_w(0.2), 60)
        self.assertEqual(_hold_deadband_w(2.0), 250)

    def test_charges_200w_even_when_pv_charges_battery(self):
        amps = _needed_charge_amps(
            0.2,
            {"grid_kw": 0.03, "battery_kw": 2.7, "soc_percent": 71.0, "load_kw": 0.5},
        )
        self.assertGreater(amps, 0)

    def test_charges_when_grid_is_below_target(self):
        rows = _charge_writes(
            _item(),
            2.0,
            {"grid_kw": 0.5, "battery_kw": 0.0, "soc_percent": 80.0},
            peak_flags=16,
        )
        values = _by_addr(rows)
        self.assertEqual(values[0x0092], TOU_WEEK_ENABLED)
        self.assertGreater(values[0x0080], 0)
        self.assertEqual(values[0x0082], 1)
        self.assertEqual(values[0x006D], MAX_DISCHARGE_AMPS)

    def test_full_battery_stops_grid_charge(self):
        amps = _needed_charge_amps(2.0, {"grid_kw": 0.4, "battery_kw": 0.0, "soc_percent": 99.0})
        self.assertEqual(amps, 0)

    def test_peak_enable_keeps_other_bits(self):
        self.assertEqual(_peak_enable_value(1), 17)
        self.assertEqual(_peak_enable_value(None), 16)
        self.assertEqual(_peak_enable_value(11002), 16)

    def test_tou_hold_sets_soc_100_and_grid_charge(self):
        values = _by_addr(_tou_hold_writes(_item(), 90))
        self.assertEqual(values[0x00A6], 90)
        self.assertEqual(values[0x00AB], 90)
        self.assertEqual(values[0x009A], TOU_HOLD_POWER_W)
        self.assertEqual(values[0x009F], TOU_HOLD_POWER_W)
        self.assertEqual(values[0x00AC], 1)
        self.assertEqual(values[0x00B1], 1)

    def test_tou_soc_follows_charge_and_discharge(self):
        self.assertEqual(
            _tou_soc_for_hold(1.5, {"load_kw": 0.8, "grid_kw": 1.45, "soc_percent": 85.0}),
            90,
        )
        self.assertEqual(
            _tou_soc_for_hold(0.5, {"load_kw": 1.2, "grid_kw": 0.5, "soc_percent": 85.0}),
            80,
        )
        self.assertEqual(
            _tou_soc_for_hold(1.0, {"load_kw": 0.95, "grid_kw": 1.0, "soc_percent": 85.0}),
            85,
        )
        self.assertEqual(
            _tou_soc_for_hold(0.2, {"load_kw": 0.5, "grid_kw": 0.03, "soc_percent": 71.0}),
            76,
        )

    def test_release_sets_zero_export_to_20_never_zero(self):
        values = _by_addr(_release_writes(_item()))
        self.assertEqual(values[0x0068], ZERO_EXPORT_IDLE_W)
        self.assertEqual(ZERO_EXPORT_IDLE_W, 20)
        self.assertNotEqual(values[0x0068], 0)
        self.assertEqual(values[0x008E], WORK_MODE_ZERO_EXPORT_CT)
        self.assertEqual(values[0x008D], ENERGY_PATTERN_LOAD_FIRST)
        self.assertEqual(values[0x00A6], TOU_HOLD_SOC)
        self.assertEqual(values[0x00AB], TOU_HOLD_SOC)
        self.assertEqual(values[0x00AC], 1)
        self.assertEqual(values[0x00B1], 1)
        self.assertEqual(values[0x009A], TOU_HOLD_POWER_W)
        self.assertEqual(values[0x0080], 0)
        self.assertEqual(values[0x0082], 0)
        self.assertEqual(values[0x00BF], 8000)

    def test_release_uses_configured_idle_state(self):
        values = _by_addr(
            _release_writes(
                _item(),
                {
                    "idle_grid_w": 35,
                    "idle_work_mode": WORK_MODE_EXPORT_FIRST,
                    "idle_energy_pattern": 0,
                    "idle_peak_w": 5000,
                    "idle_tou_soc": 40,
                    "idle_tou_charging": 0,
                    "idle_tou_power_w": 2000,
                },
            )
        )
        self.assertEqual(values[0x0068], 35)
        self.assertEqual(values[0x008E], WORK_MODE_EXPORT_FIRST)
        self.assertEqual(values[0x008D], 0)
        self.assertEqual(values[0x00BF], 5000)
        self.assertEqual(values[0x00A6], 40)
        self.assertEqual(values[0x00AB], 40)
        self.assertEqual(values[0x00AC], 0)
        self.assertEqual(values[0x00B1], 0)
        self.assertEqual(values[0x009A], 2000)
        self.assertEqual(values[0x009F], 2000)


if __name__ == "__main__":
    unittest.main()
