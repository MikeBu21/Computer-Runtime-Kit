# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest
from unittest import mock

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
    _control_item,
    _feed_writes,
    _hold_deadband_w,
    _import_writes,
    _needed_charge_amps,
    _peak_enable_row,
    _release_writes,
    _reset_hold,
    _tou_program_writes,
    simulate,
)


def _item():
    return _control_item(
        bms_seite.INVERTERS["deye_hybrid"],
        {"lookup": "sg04lp3", "model": "SUN-12K-SG04LP3"},
    )


def _by_addr(rows):
    return {int(addr): int(value) for addr, _name, value in rows}


class ImportHoldTests(unittest.TestCase):
    def setUp(self):
        _reset_hold(None)

    def test_three_phase_has_peak_tou_and_sell_registers(self):
        item = _item()
        self.assertEqual(item["register"], 0x0068)
        self.assertEqual(item["time_of_use_register"], 0x0092)
        self.assertEqual(item["grid_peak_power_register"], 0x00BF)
        self.assertEqual(item["sell_power_register"], 0x008F)

    def test_exchange_buy_charges_the_full_amount_at_once(self):
        rows, soc, charging = _import_writes(
            _item(),
            3000,
            {"grid_kw": 0.9, "battery_kw": 0.0, "load_kw": 0.4, "soc_percent": 80.0},
            16,
            immediate=True,
        )
        values = _by_addr(rows)
        self.assertEqual(values[0x00BF], 3400)
        self.assertEqual(values[0x0080], bms_seite._grid_charge_amps(3000))
        self.assertEqual(values[0x0082], 1)
        self.assertEqual((soc, charging), (100, 1))
        self.assertEqual(bms_seite._peak_trim_w, 0)

    def test_peak_shaving_is_the_target_and_tou_stays_on(self):
        rows, soc, charging = _import_writes(_item(), 2000, {"grid_kw": 0.5, "battery_kw": 0.0, "soc_percent": 80.0}, 16)
        values = _by_addr(rows)
        self.assertEqual(values[0x0092], TOU_WEEK_ENABLED)
        self.assertEqual(values[0x008E], WORK_MODE_ZERO_EXPORT_CT)
        self.assertEqual(values[0x008D], ENERGY_PATTERN_LOAD_FIRST)
        self.assertEqual(values[0x0091], 0)
        self.assertEqual(values[0x00BF], 2000)
        self.assertGreater(values[0x00E9] * 10, 2000)
        self.assertEqual(values[0x0082], 1)
        self.assertEqual(values[0x006D], MAX_DISCHARGE_AMPS)
        self.assertEqual((soc, charging), (100, 1))

    def test_charge_current_has_headroom_over_the_missing_power(self):
        # 1,5 kW fehlen, dazu Reserve. Die Kappung im Wechselrichter hält dann genau den Soll.
        amps = _needed_charge_amps(2000, {"grid_kw": 0.5, "battery_kw": 0.0, "soc_percent": 80.0})
        self.assertGreaterEqual(amps * bms_seite.PACK_VOLT, 1500 + bms_seite.CHARGE_HEADROOM_W - bms_seite.PACK_VOLT)

    def test_pv_rise_raises_charge_current_instead_of_dropping_it(self):
        target = 1000
        first = _needed_charge_amps(target, {"grid_kw": 1.0, "battery_kw": 0.5, "soc_percent": 80.0})
        # PV steigt: die Batterie lädt mehr aus PV, das Netz fällt unter den Soll.
        second = _needed_charge_amps(target, {"grid_kw": 0.2, "battery_kw": 2.3, "soc_percent": 80.0})
        self.assertGreater(second, first)

    def test_charge_current_falls_slowly(self):
        _needed_charge_amps(6000, {"grid_kw": 0.0, "battery_kw": 0.0, "soc_percent": 80.0})
        high = bms_seite._commanded_amps
        lower = _needed_charge_amps(200, {"grid_kw": 0.2, "battery_kw": 0.0, "soc_percent": 80.0})
        self.assertEqual(lower, high - bms_seite.AMP_STEP_DOWN)

    def test_full_battery_stops_grid_charge(self):
        self.assertEqual(_needed_charge_amps(2000, {"grid_kw": 0.4, "battery_kw": 0.0, "soc_percent": 99.0}), 0)

    def test_house_over_target_limits_discharge_instead_of_covering_everything(self):
        live = {"grid_kw": 3.0, "battery_kw": 0.0, "soc_percent": 80.0}
        rows, soc, _charging = _import_writes(_item(), 1000, live, 16)
        values = _by_addr(rows)
        self.assertEqual(bms_seite._hold_mode, "discharge")
        self.assertEqual(soc, TOU_HOLD_SOC)
        self.assertEqual(values[0x0080], 0)
        self.assertLess(values[0x006D], MAX_DISCHARGE_AMPS)
        self.assertGreater(values[0x006D], 0)

    def test_discharge_mode_returns_to_charge_when_house_drops(self):
        _import_writes(_item(), 1000, {"grid_kw": 3.0, "battery_kw": 0.0, "soc_percent": 80.0}, 16)
        _import_writes(_item(), 1000, {"grid_kw": 0.0, "battery_kw": -0.4, "soc_percent": 80.0}, 16)
        self.assertEqual(bms_seite._hold_mode, "charge")

    def test_peak_enable_only_adds_bit_4(self):
        self.assertEqual(_peak_enable_row(_item(), 11002), [])
        self.assertEqual(_by_addr(_peak_enable_row(_item(), 0x2AEA))[0x00B2], 0x2AFA)
        self.assertEqual(_peak_enable_row(_item(), None), [])

    def test_hold_deadband_scales_for_low_targets(self):
        self.assertEqual(_hold_deadband_w(0.2), 60)
        self.assertEqual(_hold_deadband_w(2.0), 250)

    def test_peak_cap_field_does_not_cap_the_target(self):
        self.assertEqual(simulate(0.5, 0.1, 6.0, 2.0)["target_grid_kw"], 6.0)


class FeedHoldTests(unittest.TestCase):
    def setUp(self):
        _reset_hold(None)

    def test_feed_in_uses_export_first_and_sell_cap(self):
        rows, soc, charging = _feed_writes(_item(), 2000, {"grid_kw": 0.0, "battery_kw": -0.5, "soc_percent": 80.0}, {})
        values = _by_addr(rows)
        self.assertEqual(values[0x008E], WORK_MODE_EXPORT_FIRST)
        self.assertEqual(values[0x0091], 1)
        self.assertEqual(values[0x008F], 2000)
        self.assertEqual(values[0x0080], 0)
        self.assertEqual(values[0x0082], 0)
        self.assertGreaterEqual(values[0x006D] * bms_seite.PACK_VOLT, 2500)
        self.assertEqual((soc, charging), (TOU_HOLD_SOC, bms_seite.TOU_SELL_BIT))

    def test_feed_in_switches_peak_shaving_off(self):
        live = {"grid_kw": 0.0, "battery_kw": -0.5, "soc_percent": 80.0}
        rows, _soc, _charging = _feed_writes(_item(), 2000, live, {}, 11002)
        self.assertEqual(_by_addr(rows)[0x00B2], 10986)
        rows, _soc, _charging = _feed_writes(_item(), 2000, live, {}, 10986)
        self.assertNotIn(0x00B2, _by_addr(rows))

    def test_empty_battery_stops_feed_discharge(self):
        rows, _soc, _charging = _feed_writes(_item(), 2000, {"grid_kw": 0.0, "battery_kw": 0.0, "soc_percent": 9.0}, {})
        self.assertEqual(_by_addr(rows)[0x006D], 0)


class ProgramAndReleaseTests(unittest.TestCase):
    def test_tou_program_rows(self):
        values = _by_addr(_tou_program_writes(_item(), 100, 1, with_power=True))
        self.assertEqual(values[0x00A6], 100)
        self.assertEqual(values[0x00AB], 100)
        self.assertEqual(values[0x009A], TOU_HOLD_POWER_W)
        self.assertEqual(values[0x00AC], 1)
        self.assertEqual(values[0x00B1], 1)

    def test_release_sets_zero_export_to_20_never_zero(self):
        values = _by_addr(_release_writes(_item()))
        self.assertEqual(values[0x0068], ZERO_EXPORT_IDLE_W)
        self.assertEqual(ZERO_EXPORT_IDLE_W, 20)
        self.assertEqual(values[0x008E], WORK_MODE_ZERO_EXPORT_CT)
        self.assertEqual(values[0x008D], ENERGY_PATTERN_LOAD_FIRST)
        self.assertNotIn(0x00A6, values)
        self.assertNotIn(0x00AC, values)
        self.assertNotIn(0x009A, values)
        self.assertEqual(values[0x0080], 0)
        self.assertEqual(values[0x0082], 0)
        self.assertEqual(values[0x00BF], 8000)
        self.assertEqual(values[0x0091], 0)
        self.assertEqual(values[0x0084], 0)
        self.assertEqual(values[0x008F], 13180)
        self.assertEqual(values[0x00E9], 800)
        self.assertEqual(values[0x006C], 240)
        self.assertEqual(values[0x006D], 240)
        self.assertEqual(values[0x0073], 9)

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
                    "install_max_discharge_a": 150,
                    "install_max_import_w": 6000,
                },
            )
        )
        self.assertEqual(values[0x0068], 35)
        self.assertEqual(values[0x008E], WORK_MODE_EXPORT_FIRST)
        self.assertEqual(values[0x00BF], 5000)
        self.assertEqual(values[0x00E9], 600)
        self.assertEqual(values[0x006D], 150)
        self.assertNotIn(0x00A6, values)
        self.assertNotIn(0x00AC, values)
        self.assertNotIn(0x009F, values)

    def test_release_restores_saved_program_instead_of_tou_hold(self):
        values = _by_addr(
            _release_writes(
                _item(),
                {
                    "program_soc": [35, 35, 35, 35, 35, 28],
                    "program_power": [1500, 1500, 1500, 1500, 1500, 1500],
                    "program_charging": [0, 0, 0, 0, 0, 0],
                    "idle_tou_soc": 40,
                },
            )
        )
        self.assertEqual(values[0x00A6], 35)
        self.assertEqual(values[0x00AB], 28)
        self.assertEqual(values[0x009F], 1500)
        self.assertEqual(values[0x00AC], 0)
        self.assertNotEqual(values[0x00A6], 40)
        self.assertNotEqual(values[0x00A6], TOU_HOLD_SOC)

    def test_release_clears_micro_export_cutoff_and_keeps_peak_shaving(self):
        # Bit 0 gesetzt, Bit 4 aus: Abschaltung aus, Kappung an -> nur Bit 4.
        rows = bms_seite._release_flag_row(_item(), 0x0001)
        self.assertEqual(_by_addr(rows)[0x00B2], 1 << bms_seite.GRID_PEAK_ENABLE_BIT)
        self.assertEqual(bms_seite._release_flag_row(_item(), 1 << bms_seite.GRID_PEAK_ENABLE_BIT), [])
        self.assertEqual(bms_seite._release_flag_row(_item(), None), [])

    def test_readback_reports_a_mismatch(self):
        self.assertIsNone(bms_seite.readback_diff(0x0091, "Solar Sell", 0, 0))
        text = bms_seite.readback_diff(0x0091, "Solar Sell", 0, 1)
        self.assertIn("Solar Sell", text)
        self.assertIn("gelesen 1", text)


class SocWindowTests(unittest.TestCase):
    def _settings(self):
        return {
            "soc_stop": True,
            "program_soc": [40, 20, 30, 25, 20, 35],
            "program_time": [0, 600, 1200, 1800, 2200, 2300],
            "program_power": [1000] * 6,
            "program_charging": [1] * 6,
        }

    def test_floor_is_the_lowest_of_the_six_windows(self):
        self.assertEqual(bms_seite._program_soc_floor(self._settings()), 20)

    def test_stop_limit_is_the_electricians_active_window(self):
        settings = self._settings()
        afternoon = bms_seite.datetime(2026, 11, 11, 13, 0, tzinfo=bms_seite.TZ)
        self.assertEqual(bms_seite._electrician_soc_limit(settings, afternoon), 30)
        self.assertNotEqual(bms_seite._electrician_soc_limit(settings, afternoon), 20)

    def test_active_slot_follows_the_clock_and_wraps(self):
        settings = self._settings()
        morning = bms_seite.datetime(2026, 11, 11, 10, 0, tzinfo=bms_seite.TZ)
        afternoon = bms_seite.datetime(2026, 11, 11, 13, 0, tzinfo=bms_seite.TZ)
        after_midnight = bms_seite.datetime(2026, 11, 11, 1, 0, tzinfo=bms_seite.TZ)
        self.assertEqual(bms_seite._active_program_index(settings, morning), 1)
        self.assertEqual(bms_seite._active_program_soc(settings, morning), 20)
        self.assertEqual(bms_seite._active_program_index(settings, afternoon), 2)
        self.assertEqual(bms_seite._active_program_soc(settings, afternoon), 30)
        self.assertEqual(bms_seite._active_program_index(settings, after_midnight), 0)
        later = {"program_time": [600, 800, 1200, 1800, 2200, 2300]}
        self.assertEqual(bms_seite._active_program_index(later, after_midnight), 5)

    def test_equal_start_times_use_the_highest_index(self):
        settings = {"program_time": [0, 0, 0, 0, 0, 0]}
        now = bms_seite.datetime(2026, 11, 11, 15, 0, tzinfo=bms_seite.TZ)
        self.assertEqual(bms_seite._active_program_index(settings, now), 5)

    def test_stop_at_or_below_floor_and_when_soc_is_missing(self):
        settings = self._settings()
        afternoon = bms_seite.datetime(2026, 11, 11, 13, 0, tzinfo=bms_seite.TZ)
        self.assertTrue(bms_seite._soc_floor_hit(settings, {"soc_percent": 30}, afternoon))
        self.assertTrue(bms_seite._soc_floor_hit(settings, {"soc_percent": 25}, afternoon))
        self.assertFalse(bms_seite._soc_floor_hit(settings, {"soc_percent": 31}, afternoon))
        self.assertTrue(bms_seite._soc_floor_hit(settings, {"soc_percent": None}, afternoon))
        self.assertTrue(bms_seite._soc_floor_hit({}, {"soc_percent": 80}, afternoon))

    def test_resume_needs_soc_above_the_floor_and_planning(self):
        settings = self._settings()
        now = bms_seite.datetime(2026, 11, 11, 13, 0, tzinfo=bms_seite.TZ)
        with mock.patch.object(bms_seite, "_planning_enabled", return_value=True):
            self.assertFalse(bms_seite._resume_ready(settings, {"soc_percent": 30}, now))
            self.assertTrue(bms_seite._resume_ready(settings, {"soc_percent": 31}, now))
            self.assertFalse(bms_seite._resume_ready(settings, {"soc_percent": None}, now))
        with mock.patch.object(bms_seite, "_planning_enabled", return_value=False):
            self.assertFalse(bms_seite._resume_ready(settings, {"soc_percent": 80}, now))
        stopped = dict(settings)
        stopped["soc_stop"] = False
        with mock.patch.object(bms_seite, "_planning_enabled", return_value=True):
            self.assertFalse(bms_seite._resume_ready(stopped, {"soc_percent": 80}, now))

    def test_gate_clears_soc_stop_only_above_floor_while_planning_stays_on(self):
        settings = self._settings()
        settings["real_mode"] = False
        settings["release_pending"] = False

        def remember(updates):
            merged = dict(settings)
            merged.update(updates)
            return merged

        afternoon = bms_seite.datetime(2026, 11, 11, 13, 0, tzinfo=bms_seite.TZ)
        with mock.patch.object(bms_seite, "_planning_enabled", return_value=True), mock.patch.object(
            bms_seite, "save_settings", side_effect=remember
        ) as save:
            resumed = bms_seite.apply_soc_gate(settings, {"soc_percent": 31}, afternoon)
        self.assertFalse(resumed["soc_stop"])
        save.assert_called_once_with({"soc_stop": False})

        with mock.patch.object(bms_seite, "_planning_enabled", return_value=True), mock.patch.object(
            bms_seite, "save_settings", side_effect=remember
        ) as save:
            held = bms_seite.apply_soc_gate(settings, {"soc_percent": 30}, afternoon)
        self.assertTrue(held["soc_stop"])
        save.assert_not_called()

        with mock.patch.object(bms_seite, "_planning_enabled", return_value=False), mock.patch.object(
            bms_seite, "save_settings", side_effect=remember
        ) as save:
            parked = bms_seite.apply_soc_gate(settings, {"soc_percent": 80})
        self.assertTrue(parked["soc_stop"])
        save.assert_not_called()

    def test_planning_restarts_writes_after_the_soc_stop_lifts(self):
        base = {
            "soc_stop": False,
            "planning_hold": True,
            "real_mode": False,
            "community_hold": False,
            "exchange_hold": False,
            "target_grid_kw": 1.0,
            "program_soc": [40, 20, 30, 25, 20, 35],
            "program_time": [0, 600, 1200, 1800, 2200, 2300],
        }
        morning = bms_seite.datetime(2026, 11, 11, 10, 0, tzinfo=bms_seite.TZ)
        afternoon = bms_seite.datetime(2026, 11, 11, 13, 0, tzinfo=bms_seite.TZ)

        def remember(updates):
            merged = dict(base)
            merged.update(updates)
            return merged

        with mock.patch("energie_steuerung.load_settings", return_value={"algorithmische_planung": True}), mock.patch(
            "grundverbrauch_rechner.recommended_import_kw", return_value=1.2
        ), mock.patch.object(bms_seite, "save_settings", side_effect=remember) as save:
            started = bms_seite.apply_planning_hold(base, {"soc_percent": 21}, morning)
        self.assertTrue(started["real_mode"])
        self.assertTrue(started["planning_hold"])
        self.assertEqual(started["target_grid_kw"], 1.2)
        save.assert_called_once()

        blocked = dict(base)
        blocked["soc_stop"] = True
        with mock.patch("energie_steuerung.load_settings", return_value={"algorithmische_planung": True}), mock.patch(
            "grundverbrauch_rechner.recommended_import_kw", return_value=1.2
        ), mock.patch.object(bms_seite, "save_settings", side_effect=remember) as save:
            stayed = bms_seite.apply_planning_hold(blocked, {"soc_percent": 21}, morning)
        self.assertTrue(stayed["soc_stop"])
        self.assertFalse(stayed["real_mode"])
        save.assert_not_called()

        running = dict(base)
        running["real_mode"] = True
        running["target_grid_kw"] = 1.2

        def remember_running(updates):
            merged = dict(running)
            merged.update(updates)
            return merged

        with mock.patch("energie_steuerung.load_settings", return_value={"algorithmische_planung": True}), mock.patch(
            "grundverbrauch_rechner.recommended_import_kw", return_value=1.2
        ), mock.patch.object(bms_seite, "save_settings", side_effect=remember_running) as save:
            stopped = bms_seite.apply_planning_hold(running, {"soc_percent": 25}, afternoon)
        self.assertFalse(stopped["real_mode"])
        self.assertTrue(stopped["soc_stop"])
        save.assert_called_once_with({"real_mode": False, "soc_stop": True})

    def test_autocapture_only_on_a_new_control_serial(self):
        self.assertTrue(bms_seite._should_autocapture(0, 123, {"install_captured": False}))
        self.assertFalse(
            bms_seite._should_autocapture(123, 123, {"install_captured": False, "install_captured_serial": 0})
        )
        self.assertFalse(
            bms_seite._should_autocapture(123, 123, {"install_captured": True, "install_captured_serial": 123})
        )
        self.assertTrue(
            bms_seite._should_autocapture(123, 456, {"install_captured": True, "install_captured_serial": 123})
        )


class InstallerCeilingTests(unittest.TestCase):
    def setUp(self):
        _reset_hold(None)

    def test_writes_never_exceed_installer_values(self):
        settings = {
            "install_max_charge_a": 100,
            "install_max_discharge_a": 120,
            "install_max_sell_w": 4000,
            "install_max_import_w": 5000,
        }
        rows = [
            (0x0080, "Battery Grid Charging Current", 240),
            (0x006D, "Battery Max Discharging Current", 240),
            (0x008F, "Export Surplus Power", 6000),
            (0x00BF, "Grid Peak shaving", 6000),
            (0x00E9, "Grid Max Import power", 800),
            (0x0068, "Zero Export power", 20),
        ]
        values = _by_addr(bms_seite._cap_rows(rows, settings))
        self.assertEqual(values[0x0080], 100)
        self.assertEqual(values[0x006D], 120)
        self.assertEqual(values[0x008F], 4000)
        self.assertEqual(values[0x00BF], 5000)
        self.assertEqual(values[0x00E9], 500)
        self.assertEqual(values[0x0068], 20)

    def test_feed_sell_cap_is_the_target_without_trim(self):
        live = {"grid_kw": -2.6, "battery_kw": -3.0, "soc_percent": 80.0}
        rows, _soc, _charging = _feed_writes(_item(), 3000, live, {})
        self.assertEqual(_by_addr(rows)[0x008F], 3000)

    def test_planning_skips_values_below_a_fifth_of_a_kilowatt(self):
        self.assertIsNone(bms_seite.planning_write_kw(0.02))
        self.assertIsNone(bms_seite.planning_write_kw(0.018))
        self.assertIsNone(bms_seite.planning_write_kw(0.19))
        self.assertEqual(bms_seite.planning_write_kw(0.2), 0.2)
        self.assertEqual(bms_seite.planning_write_kw(1.25), 1.25)

    def test_peak_cap_follows_target_automatically(self):
        self.assertEqual(bms_seite._auto_peak_kw({"target_grid_kw": 2.0, "idle_peak_w": 8000}), 2.0)
        self.assertEqual(bms_seite._auto_peak_kw({"target_grid_kw": -3.0, "idle_peak_w": 8000}), 8.0)


if __name__ == "__main__":
    unittest.main()
