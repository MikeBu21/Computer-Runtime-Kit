# -*- coding: utf-8 -*-
from __future__ import annotations

import struct
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
        self.assertEqual(values[0x0091], 1)
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
        self.assertEqual(values[0x0091], 1)
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

    def test_solarman_wait_timeout_is_one_missing_reply(self):
        sock = mock.Mock()
        sock.recv.side_effect = TimeoutError("timed out")
        modbus = bms_seite._modbus_read_request(1, 0x006C, 2)
        with self.assertRaises(OSError) as caught:
            bms_seite._solarman_exchange(
                "127.0.0.1", 8899, 1, modbus, bms_seite._accept_read(1, 2), sock=sock
            )
        self.assertEqual(str(caught.exception), "keine passende Logger-Antwort")
        sock.recv.assert_called_once()

    def test_reply_after_a_stray_frame_start_is_still_accepted(self):
        """Ein 0xA5 mitten in fremdem Verkehr darf die eigene Antwort nicht verschlucken."""
        sent = {}

        class Sock:
            def sendall(self, data):
                sent["frame"] = data

            def settimeout(self, _timeout):
                return None

            def recv(self, _size):
                if sent.get("done"):
                    raise TimeoutError("timed out")
                sent["done"] = True
                reply = bytearray.fromhex(
                    "a515001015dafb30dac6b002014e8ce403650b000059e216670103020014b84b9815"
                )
                reply[5] = sent["frame"][5]
                reply[-2] = sum(reply[1:-2]) & 0xFF
                # Länge 0x0FFF wäre über 600 und hat früher den Rest der Wartezeit blockiert.
                return bytes.fromhex("a5ff0f") + bytes(reply)

            def close(self):
                return None

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

        with mock.patch.object(bms_seite, "_unicast_discover", return_value=None), mock.patch(
            "bms_seite.socket.create_connection", return_value=Sock()
        ):
            words = bms_seite.read_solarman_span("10.2.2.8", 8899, 1, 818181, 0x0068, 1)
        self.assertEqual(words, [20])

    def test_reread_uses_the_logger_announced_at_that_ip(self):
        """Nicht die feste Nummer aus einem anderen Netz, sondern die des Loggers an der IP."""
        other_host = "10.8.0.20"
        other_serial = 555001
        sent = {}

        class Sock:
            def sendall(self, data):
                sent["frame"] = data

            def settimeout(self, _timeout):
                return None

            def recv(self, _size):
                if sent.get("done"):
                    raise TimeoutError("timed out")
                sent["done"] = True
                reply = bytearray.fromhex(
                    "a515001015dafb30dac6b002014e8ce403650b000059e216670103020014b84b9815"
                )
                reply[5] = sent["frame"][5]
                reply[-2] = sum(reply[1:-2]) & 0xFF
                return bytes(reply)

            def close(self):
                return None

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

        bms_seite._logger_serial_cache.clear()
        discovered = {
            "ip": other_host,
            "mac": "aa:bb:cc:dd:ee:11",
            "serial": other_serial,
            "port": 8899,
        }
        with mock.patch.object(bms_seite, "_unicast_discover", return_value=discovered), mock.patch(
            "bms_seite.socket.create_connection", return_value=Sock()
        ):
            words = bms_seite.read_solarman_span(other_host, 8899, 1, 2405169065, 0x0068, 1)
        self.assertEqual(words, [20])
        packed = struct.unpack_from("<I", sent["frame"], 7)[0]
        self.assertEqual(packed, other_serial)
        self.assertNotEqual(packed, 2965822000)
        self.assertNotEqual(packed, 2405169065)

    def test_span_retries_when_the_first_socket_stays_silent(self):
        """Ein stiller erster Versuch wird auf einer neuen Verbindung wiederholt. Andere IP, andere Nummer."""
        host = "10.9.9.40"
        serial = 777001
        sockets = []

        class Sock:
            def __init__(self, answer: bool):
                self.answer = answer
                self.sent = b""
                self.used = False

            def sendall(self, data):
                self.sent += data

            def settimeout(self, _timeout):
                return None

            def recv(self, _size):
                if not self.answer or self.used:
                    raise TimeoutError("timed out")
                self.used = True
                reply = bytearray.fromhex(
                    "a515001015dafb30dac6b002014e8ce403650b000059e216670103020014b84b9815"
                )
                reply[5] = self.sent[5]
                reply[-2] = sum(reply[1:-2]) & 0xFF
                return bytes(reply)

            def close(self):
                return None

        def connect(*_args, **_kwargs):
            sock = Sock(answer=True)
            sockets.append(sock)
            return sock

        link = bms_seite._LoggerRead(Sock(answer=False), host, 8899, 1, serial)
        with mock.patch("bms_seite.socket.create_connection", side_effect=connect):
            words = link.read_span(0x0068, 1, attempts=2, timeout=0.2)
        self.assertEqual(words, [20])
        self.assertEqual(len(sockets), 1)
        self.assertEqual(link.host, host)
        self.assertEqual(link.serial, serial)

    def test_capture_reads_whichever_inverter_is_configured(self):
        other_host = "10.4.4.12"
        other_serial = 424242
        settings = {
            "modbus_host": other_host,
            "modbus_port": 8899,
            "modbus_unit": 1,
            "logger_ip": other_host,
            "logger_serial": 2965822000,
            "inverter": "deye_hybrid",
            "real_mode": True,
            "known_entry_ids": [],
            "tracked_inverters": [{"ip": other_host, "serial": 2965822000, "control": True}],
        }
        seen = {}

        class Link:
            def __init__(self, _sock, host, _port, _unit, serial):
                seen["host"] = host
                seen["serial"] = serial

            def close(self):
                return None

            def read_span(self, address, count):
                return [1] * count

        bms_seite._logger_serial_cache.clear()
        discovered = {"ip": other_host, "mac": "aa:bb:cc:dd:ee:22", "serial": other_serial, "port": 8899}
        with mock.patch.object(bms_seite, "load_settings", return_value=settings), mock.patch.object(
            bms_seite, "_unicast_discover", return_value=discovered
        ), mock.patch.object(
            bms_seite, "resolve_ha_link", return_value={"serial": 2405169065, "host": other_host}
        ), mock.patch.object(
            bms_seite._LoggerRead, "connect", side_effect=lambda *_args, **_kwargs: Link(None, other_host, 8899, 1, other_serial)
        ) as connect, mock.patch.object(bms_seite, "save_settings") as save:
            # connect is mocked, so assert the serial capture would pass in.
            result = bms_seite.capture_install_from_inverter()
        self.assertTrue(result["ok"], result.get("message"))
        save.assert_not_called()
        connect.assert_called_once()
        self.assertEqual(connect.call_args.args[1], other_serial)
        self.assertNotEqual(connect.call_args.args[1], 2965822000)
        self.assertEqual(result["settings"]["install_captured_serial"], other_serial)

    def test_readback_reports_a_mismatch(self):
        self.assertIsNone(bms_seite.readback_diff(0x0091, "Solar Sell", 0, 0))
        text = bms_seite.readback_diff(0x0091, "Solar Sell", 0, 1)
        self.assertIn("Solar Sell", text)
        self.assertIn("gelesen 1", text)

    def test_normalbetrieb_drift_detects_solar_sell_mismatch_and_peak(self):
        # Elektriker-Soll an: "on" passt, "off" driftet.
        with mock.patch(
            "bms_seite.read_register_state",
            return_value={"solar_sell": "off", "peak_w": "12000"},
        ):
            self.assertTrue(
                bms_seite._normalbetrieb_drift({"idle_peak_w": 12000, "install_solar_sell": 1})
            )
        with mock.patch(
            "bms_seite.read_register_state",
            return_value={"solar_sell": "on", "peak_w": "12000"},
        ):
            self.assertFalse(
                bms_seite._normalbetrieb_drift({"idle_peak_w": 12000, "install_solar_sell": 1})
            )
        with mock.patch(
            "bms_seite.read_register_state",
            return_value={"solar_sell": "on", "peak_w": "12880"},
        ):
            self.assertTrue(
                bms_seite._normalbetrieb_drift({"idle_peak_w": 12000, "install_solar_sell": 1})
            )
        with mock.patch(
            "bms_seite.read_register_state",
            return_value={"solar_sell": "off", "peak_w": "12000"},
        ):
            self.assertFalse(
                bms_seite._normalbetrieb_drift({"idle_peak_w": 12000, "install_solar_sell": 0})
            )

    def test_saved_soc_below_20_is_kept_and_written_back(self):
        self.assertEqual(bms_seite._idle_tou_soc({"idle_tou_soc": 13}), 13)
        values = _by_addr(
            _release_writes(
                _item(),
                {
                    "program_soc": [13, 13, 13, 13, 13, 13],
                    "program_power": [900] * 6,
                    "program_charging": [0] * 6,
                    "install_grid_charge": 1,
                    "install_grid_charge_a": 15,
                    "install_microinverter": 1,
                    "install_time_of_use": 0,
                },
            )
        )
        self.assertEqual(values[0x00A6], 13)
        self.assertEqual(values[0x0080], 15)
        self.assertEqual(values[0x0082], 1)
        self.assertEqual(values[0x0084], 1)
        self.assertEqual(values[0x0092], 0)

    def test_planning_stop_turns_register_writes_off(self):
        settings = {"real_mode": True, "planning_hold": True, "community_hold": False, "exchange_hold": False}
        with mock.patch("energie_steuerung.load_settings", return_value={"algorithmische_planung": False}), mock.patch.object(
            bms_seite, "save_settings", side_effect=lambda updates: {**settings, **updates}
        ) as save:
            result = bms_seite.apply_planning_hold(settings)
        save.assert_called_once_with({"real_mode": False, "planning_hold": False})
        self.assertFalse(result["real_mode"])

    def test_release_restores_saved_solar_sell(self):
        values = _by_addr(_release_writes(_item(), {"install_solar_sell": 1}))
        self.assertEqual(values[0x0091], 1)
        values = _by_addr(_release_writes(_item(), {"install_solar_sell": 0}))
        self.assertEqual(values[0x0091], 0)

    def test_import_writes_saved_solar_sell(self):
        rows, _soc, _ch = _import_writes(
            _item(),
            2000,
            {"grid_kw": 0.5, "battery_kw": 0.0, "soc_percent": 80.0},
            16,
            settings={"install_solar_sell": 1},
        )
        self.assertEqual(_by_addr(rows)[0x0091], 1)
        rows, _soc, _ch = _import_writes(
            _item(),
            2000,
            {"grid_kw": 0.5, "battery_kw": 0.0, "soc_percent": 80.0},
            16,
            settings={"install_solar_sell": 0},
        )
        self.assertEqual(_by_addr(rows)[0x0091], 0)

    def test_queue_release_if_drift_sets_pending_once(self):
        bms_seite._last_drift_release = 0.0
        bms_seite._last_drift_check = 0.0
        bms_seite._release_cooldown_until = 0.0
        bms_seite._release_sent = True
        settings = {"real_mode": False, "release_pending": False, "idle_peak_w": 12000}
        fresh = dict(settings)
        with mock.patch(
            "bms_seite.read_register_state",
            return_value={"solar_sell": "on", "peak_w": "12880"},
        ), mock.patch.object(bms_seite, "load_settings", return_value=fresh), mock.patch(
            "bms_seite._persist_settings"
        ) as persist:
            self.assertTrue(bms_seite._queue_release_if_drift(settings))
            self.assertTrue(settings["release_pending"])
            self.assertFalse(bms_seite._release_sent)
            persist.assert_called()
            self.assertFalse(bms_seite._queue_release_if_drift(settings))


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

    def test_checkbox_stays_on_without_a_snapshot_when_soc_is_known(self):
        settings = {
            "real_mode": True,
            "soc_stop": False,
            "release_pending": True,
            "program_soc": [],
            "program_time": [],
            "program_power": [],
            "program_charging": [],
        }
        with mock.patch.object(bms_seite, "save_settings") as save:
            kept = bms_seite.apply_soc_gate(settings, {"soc_percent": 55})
        self.assertTrue(kept["real_mode"])
        self.assertFalse(kept["soc_stop"])
        save.assert_not_called()

        latched = dict(settings)
        latched["soc_stop"] = True

        def remember(updates):
            merged = dict(latched)
            merged.update(updates)
            return merged

        with mock.patch.object(bms_seite, "save_settings", side_effect=remember) as save:
            cleared = bms_seite.apply_soc_gate(latched, {"soc_percent": 55})
        self.assertTrue(cleared["real_mode"])
        self.assertFalse(cleared["soc_stop"])
        save.assert_called_once_with({"soc_stop": False})

    def test_missing_soc_does_not_uncheck_or_latch(self):
        settings = self._settings()
        settings["real_mode"] = True
        settings["soc_stop"] = False
        settings["release_pending"] = False
        afternoon = bms_seite.datetime(2026, 11, 11, 13, 0, tzinfo=bms_seite.TZ)
        with mock.patch.object(bms_seite, "save_settings") as save:
            kept = bms_seite.apply_soc_gate(settings, {"soc_percent": None}, afternoon)
        self.assertTrue(kept["real_mode"])
        self.assertFalse(kept["soc_stop"])
        save.assert_not_called()

    def test_real_window_still_turns_the_checkbox_off(self):
        settings = self._settings()
        settings["real_mode"] = True
        settings["soc_stop"] = False
        settings["release_pending"] = False
        afternoon = bms_seite.datetime(2026, 11, 11, 13, 0, tzinfo=bms_seite.TZ)

        def remember(updates):
            merged = dict(settings)
            merged.update(updates)
            return merged

        with mock.patch.object(bms_seite, "save_settings", side_effect=remember) as save:
            stopped = bms_seite.apply_soc_gate(settings, {"soc_percent": 25}, afternoon)
        self.assertFalse(stopped["real_mode"])
        self.assertTrue(stopped["soc_stop"])
        save.assert_called_once_with({"real_mode": False, "soc_stop": True})

    def test_release_pending_does_not_block_resume_above_the_floor(self):
        settings = self._settings()
        settings["real_mode"] = True
        settings["soc_stop"] = True
        settings["release_pending"] = True
        afternoon = bms_seite.datetime(2026, 11, 11, 13, 0, tzinfo=bms_seite.TZ)

        def remember(updates):
            merged = dict(settings)
            merged.update(updates)
            return merged

        with mock.patch.object(bms_seite, "_planning_enabled", return_value=True), mock.patch.object(
            bms_seite, "save_settings", side_effect=remember
        ) as save:
            resumed = bms_seite.apply_soc_gate(settings, {"soc_percent": 31}, afternoon)
        self.assertTrue(resumed["real_mode"])
        self.assertFalse(resumed["soc_stop"])
        save.assert_called_once_with({"soc_stop": False})

    def test_enabling_without_a_floor_clears_soc_stop(self):
        data = bms_seite.default_settings()
        data["real_mode"] = False
        data["soc_stop"] = True
        data["modbus_host"] = ""
        with mock.patch.object(bms_seite, "load_settings", return_value=data), mock.patch.object(
            bms_seite, "_persist_settings"
        ) as persist, mock.patch.object(bms_seite, "_apply_ha_link", side_effect=lambda item: item), mock.patch.object(
            bms_seite, "_live_soc", return_value=40.0
        ), mock.patch.object(bms_seite, "_electrician_soc_limit", return_value=None):
            saved = bms_seite.save_settings({"real_mode": True})
        self.assertTrue(saved["real_mode"])
        self.assertFalse(saved["soc_stop"])
        persist.assert_called_once()


class InstallAutocaptureTests(unittest.TestCase):
    def setUp(self):
        bms_seite._reset_install_autocapture()

    def _programs(self):
        return {
            "program_soc": [40, 20, 30, 25, 20, 35],
            "program_power": [1000] * 6,
            "program_charging": [1] * 6,
            "program_time": [0, 600, 1200, 1800, 2200, 2300],
        }

    def _valid(self, serial=123):
        data = {
            "logger_serial": serial,
            "install_captured": True,
            "install_captured_serial": serial,
            "real_mode": False,
            "release_pending": False,
            "soc_stop": False,
        }
        data.update(self._programs())
        return data

    def _missing(self, serial=123):
        return {
            "logger_serial": serial,
            "install_captured": False,
            "install_captured_serial": 0,
            "real_mode": False,
            "release_pending": False,
            "soc_stop": False,
            "program_soc": [],
            "program_power": [],
            "program_charging": [],
            "program_time": [],
        }

    def test_no_autocapture_when_the_same_serial_is_already_captured(self):
        settings = self._valid()
        with mock.patch.object(bms_seite, "capture_install_from_inverter") as capture, mock.patch.object(
            bms_seite, "load_settings"
        ) as load:
            result = bms_seite.maybe_autocapture_install(settings)
        capture.assert_not_called()
        load.assert_not_called()
        self.assertTrue(bms_seite._install_snapshot_valid(result))

    def test_saved_snapshot_is_not_read_again_from_stale_memory(self):
        saved = self._valid(2965822000)
        with mock.patch.object(bms_seite, "load_settings", return_value=saved), mock.patch.object(
            bms_seite, "capture_install_from_inverter"
        ) as capture:
            result = bms_seite.maybe_autocapture_install(self._missing(2965822000))
        capture.assert_not_called()
        self.assertTrue(bms_seite._install_snapshot_valid(result))
        self.assertEqual(result["install_captured_serial"], 2965822000)

    def test_one_autocapture_when_the_snapshot_is_missing_and_bms_is_idle(self):
        missing = self._missing()
        saved = self._valid()
        with mock.patch.object(bms_seite, "load_settings", return_value=dict(missing)), mock.patch.object(
            bms_seite, "capture_install_from_inverter", return_value={"ok": True, "settings": saved}
        ) as capture:
            first = bms_seite.maybe_autocapture_install(dict(missing))
            second = bms_seite.maybe_autocapture_install(first)
        self.assertEqual(capture.call_count, 1)
        self.assertTrue(bms_seite._install_snapshot_valid(second))

    def test_incomplete_or_wrong_serial_counts_as_missing(self):
        wrong_serial = self._valid()
        wrong_serial["install_captured_serial"] = 999
        incomplete = self._valid()
        incomplete["program_soc"] = [20, 20]
        self.assertFalse(bms_seite._install_snapshot_valid(wrong_serial))
        self.assertFalse(bms_seite._install_snapshot_valid(incomplete))
        self.assertFalse(bms_seite._install_snapshot_valid(self._missing()))

    def test_locked_release_does_not_block_reread(self):
        bms_seite._reset_install_autocapture()
        bms_seite._clear_release_block()
        settings = self._missing()
        settings["release_pending"] = True
        settings["real_mode"] = False
        bms_seite._release_block_key = bms_seite._release_settings_key(settings)
        bms_seite._release_block_gap = ("on", "12880")
        self.assertFalse(bms_seite._bms_is_writing(settings))
        with mock.patch.object(
            bms_seite, "capture_install_from_inverter", return_value={"ok": False, "message": "später"}
        ) as capture, mock.patch.object(bms_seite, "load_settings", return_value=dict(settings)):
            bms_seite.maybe_autocapture_install(dict(settings))
        capture.assert_called_once()
        bms_seite._clear_release_block()

    def test_no_read_while_real_mode_or_release_pending(self):
        bms_seite._clear_release_block()
        for flag in ("real_mode", "release_pending"):
            bms_seite._reset_install_autocapture()
            settings = self._missing()
            settings[flag] = True
            with mock.patch.object(bms_seite, "capture_install_from_inverter") as capture, mock.patch.object(
                bms_seite, "load_settings"
            ) as load:
                result = bms_seite.maybe_autocapture_install(settings)
            capture.assert_not_called()
            load.assert_not_called()
            self.assertTrue(result[flag])

        blocked = self._missing()
        blocked["real_mode"] = True
        blocked["modbus_host"] = "192.168.1.176"
        blocked["logger_serial"] = 123

        class _FakeLink:
            def __init__(self):
                self.calls = 0

            def read_span(self, address, count):
                self.calls += 1
                return [
                    13 if address + offset == bms_seite.REG_PROGRAM_SOC else 1
                    for offset in range(count)
                ]

            def close(self):
                return None

        fake = _FakeLink()
        with mock.patch.object(bms_seite, "load_settings", return_value=blocked), mock.patch.object(
            bms_seite._LoggerRead, "connect", return_value=fake
        ), mock.patch.object(bms_seite, "save_settings") as save:
            result = bms_seite.capture_install_from_inverter()
        self.assertGreater(fake.calls, 1)
        save.assert_not_called()
        self.assertTrue(result["ok"])
        self.assertFalse(result["saved"])
        self.assertEqual(result["settings"]["program_soc"][0], 13)

    def test_reread_fills_every_electrician_field(self):
        blocked = self._missing()
        blocked["real_mode"] = True
        blocked["modbus_host"] = "192.168.1.176"
        blocked["logger_serial"] = 123
        words = {
            0x0068: 20,
            0x006C: 240,
            0x006D: 240,
            0x0073: 9,
            0x0074: 10,
            0x0075: 11,
            0x0080: 40,
            0x0081: 0,
            0x0082: 1,
            0x0083: 0,
            0x0084: 0,
            0x008D: 1,
            0x008E: 2,
            0x008F: 13180,
            0x0090: 0,
            0x0091: 1,
            0x0092: 255,
            0x00B2: 1 << bms_seite.GRID_PEAK_ENABLE_BIT,
            0x00BF: 8000,
            0x00E9: 800,
        }
        for index in range(6):
            words[bms_seite.REG_PROGRAM_TIME + index] = 100 + index
            words[bms_seite.REG_PROGRAM_POWER + index] = 5000 + index
            words[bms_seite.REG_PROGRAM_SOC + index] = 20
            words[bms_seite.REG_PROGRAM_CHARGE + index] = index

        class _FakeLink:
            def read_span(self, address, count):
                return [words[address + offset] for offset in range(count)]

            def close(self):
                return None

        with mock.patch.object(bms_seite, "load_settings", return_value=blocked), mock.patch.object(
            bms_seite._LoggerRead, "connect", return_value=_FakeLink()
        ), mock.patch.object(bms_seite, "save_settings") as save:
            result = bms_seite.capture_install_from_inverter()
        save.assert_not_called()
        shown = result["settings"]
        self.assertEqual(shown["idle_grid_w"], 20)
        self.assertEqual(shown["idle_work_mode"], 2)
        self.assertEqual(shown["idle_energy_pattern"], 1)
        self.assertEqual(shown["idle_peak_w"], 8000)
        self.assertEqual(shown["install_max_charge_a"], 240)
        self.assertEqual(shown["install_max_discharge_a"], 240)
        self.assertEqual(shown["install_max_sell_w"], 13180)
        self.assertEqual(shown["install_max_import_w"], 8000)
        self.assertEqual(shown["install_solar_sell"], 1)
        self.assertEqual(shown["install_shutdown_soc"], 9)
        self.assertEqual(shown["install_restart_soc"], 10)
        self.assertEqual(shown["install_low_soc"], 11)
        self.assertEqual(shown["install_time_of_use"], 255)
        self.assertEqual(shown["install_grid_charge"], 1)
        self.assertEqual(shown["install_grid_charge_a"], 40)
        self.assertEqual(shown["install_microinverter"], 0)
        self.assertEqual(shown["install_peak_shaving"], 1)
        self.assertEqual(shown["install_export_cutoff"], 0)
        self.assertEqual(shown["program_soc"], [20, 20, 20, 20, 20, 20])
        self.assertEqual(shown["program_power"], [5000, 5001, 5002, 5003, 5004, 5005])
        self.assertEqual(shown["program_charging"], [0, 1, 2, 3, 4, 5])

    def test_failed_read_retries_once_later_not_every_tick(self):
        missing = self._missing()
        clock = {"now": 1000.0}
        with mock.patch.object(bms_seite, "load_settings", return_value=dict(missing)), mock.patch.object(
            bms_seite, "capture_install_from_inverter", return_value={"ok": False, "message": "timeout"}
        ) as capture, mock.patch.object(bms_seite.time, "monotonic", side_effect=lambda: clock["now"]):
            bms_seite.maybe_autocapture_install(dict(missing))
            bms_seite.maybe_autocapture_install(dict(missing))
            self.assertEqual(capture.call_count, 1)
            clock["now"] = 1000.0 + bms_seite.AUTOCAPTURE_RETRY_SEC
            bms_seite.maybe_autocapture_install(dict(missing))
            self.assertEqual(capture.call_count, 2)
            clock["now"] = 1000.0 + 4 * bms_seite.AUTOCAPTURE_RETRY_SEC
            bms_seite.maybe_autocapture_install(dict(missing))
            self.assertEqual(capture.call_count, 2)


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


class SocImportRampTests(unittest.TestCase):
    def setUp(self):
        bms_seite.reset_netzbezug_ramp_state()

    def _settings(self, max_import_w, target_kw=0.4):
        return {
            "real_mode": True,
            "planning_hold": False,
            "community_hold": False,
            "exchange_hold": False,
            "soc_stop": False,
            "release_pending": True,
            "install_captured": True,
            "install_captured_serial": 2965822000,
            "target_grid_kw": target_kw,
            "install_max_import_w": max_import_w,
            "program_soc": [40, 20, 30, 25, 20, 35],
            "program_time": [0, 600, 1200, 1800, 2200, 2300],
            "program_power": [1000] * 6,
            "program_charging": [1] * 6,
        }

    def _settings_sockel(self, max_import_w, sockel, target_kw=0.4):
        settings = self._settings(max_import_w, target_kw=target_kw)
        settings["program_soc"] = [sockel] * 6
        return settings

    def _enter_falling(self, sockel, max_import_w, above=None):
        """Von oberhalb der Schwelle ins Fenster fallen, damit die Rampe aktiv wird."""
        schwelle = bms_seite.netzbezug_rampe_schwelle(sockel)
        start = schwelle + 1 if above is None else above
        self.assertIsNone(bms_seite.netzbezug_soc_rampe_kw(start, sockel, max_import_w))

    def test_threshold_is_sockel_plus_ten(self):
        self.assertEqual(bms_seite.netzbezug_rampe_schwelle(20), 30)
        self.assertEqual(bms_seite.netzbezug_rampe_schwelle(30), 40)
        self.assertEqual(bms_seite.netzbezug_rampe_schwelle(35), 45)
        self.assertEqual(bms_seite.netzbezug_rampe_schwelle(None), 30)
        morning = bms_seite.datetime(2026, 11, 14, 10, 0, tzinfo=bms_seite.TZ)
        afternoon = bms_seite.datetime(2026, 11, 14, 13, 0, tzinfo=bms_seite.TZ)
        settings = self._settings(8000)
        self.assertEqual(bms_seite._electrician_soc_limit(settings, morning), 20)
        self.assertEqual(
            bms_seite.netzbezug_rampe_schwelle(bms_seite._electrician_soc_limit(settings, morning)),
            30,
        )
        self.assertEqual(bms_seite._electrician_soc_limit(settings, afternoon), 30)
        self.assertEqual(
            bms_seite.netzbezug_rampe_schwelle(bms_seite._electrician_soc_limit(settings, afternoon)),
            40,
        )

    def test_ramp_never_raises_import_at_any_soc(self):
        """Fall unter Sockel+10 erzeugt keine lineare Leistung mehr, auch nicht bis 12 kW."""
        for soc, sockel, watts in (
            (30, 20, 12000),
            (29, 20, 12000),
            (25, 20, 12000),
            (22, 20, 8000),
            (22, 20, 3000),
            (40, 30, 12000),
            (35, 30, 12000),
            (45, 35, 8000),
            (39, 30, 8000),
            (21, 20, 12000),
        ):
            self._enter_falling(sockel, watts)
            self.assertIsNone(bms_seite.netzbezug_soc_rampe_kw(soc, sockel, watts))

    def test_planner_stays_the_only_setpoint_inside_the_old_ramp_window(self):
        morning = bms_seite.datetime(2026, 11, 14, 10, 0, tzinfo=bms_seite.TZ)
        afternoon = bms_seite.datetime(2026, 11, 14, 13, 0, tzinfo=bms_seite.TZ)
        cases = (
            (self._settings_sockel(12000, sockel=20, target_kw=0.4), 25, morning, 0.4),
            (self._settings_sockel(12000, sockel=20, target_kw=0.4), 22, morning, 0.4),
            (self._settings_sockel(8000, sockel=20, target_kw=0.2), 22, morning, 0.2),
            (self._settings_sockel(3000, sockel=20, target_kw=0.2), 22, morning, 0.2),
            (self._settings_sockel(12000, sockel=30, target_kw=0.5), 40, morning, 0.5),
            (self._settings_sockel(12000, sockel=30, target_kw=0.5), 35, morning, 0.5),
            (self._settings_sockel(12000, sockel=30, target_kw=0.5), 30, morning, 0.5),
            (self._settings_sockel(12000, sockel=20, target_kw=3.0), 30, morning, 3.0),
            (self._settings_sockel(12000, sockel=20, target_kw=0.4), 21, morning, 0.4),
            (self._settings(8000, target_kw=0.5), 39, afternoon, 0.5),
            (self._settings_sockel(8000, sockel=20, target_kw=2.0), 31, morning, 2.0),
        )
        for settings, soc, now, expected in cases:
            kw = bms_seite.soll_netzbezug_kw(
                settings, {"soc_percent": soc}, planning_on=True, now=now
            )
            self.assertEqual(kw, expected)
            self.assertNotEqual(kw, 12.0)
            self.assertLess(kw, 12.0)

    def test_planning_off_blocks_grid_import(self):
        morning = bms_seite.datetime(2026, 11, 14, 10, 0, tzinfo=bms_seite.TZ)
        settings = self._settings_sockel(12000, sockel=20, target_kw=0.4)
        settings["real_mode"] = False
        settings["planning_hold"] = False
        settings["community_hold"] = False
        settings["exchange_hold"] = False
        with mock.patch.object(bms_seite, "save_settings") as save, mock.patch.object(
            bms_seite, "capture_install_from_inverter"
        ) as capture, mock.patch.object(bms_seite, "load_settings") as load:
            off_above = bms_seite.soll_netzbezug_kw(
                settings, {"soc_percent": 50}, planning_on=False, now=morning
            )
            off_low = bms_seite.soll_netzbezug_kw(
                settings, {"soc_percent": 25}, planning_on=False, now=morning
            )
            on_low = bms_seite.soll_netzbezug_kw(
                settings, {"soc_percent": 25}, planning_on=True, now=morning
            )
        self.assertEqual(off_above, 0.0)
        self.assertEqual(off_low, 0.0)
        self.assertEqual(on_low, 0.4)
        self.assertEqual(settings["target_grid_kw"], 0.4)
        save.assert_not_called()
        capture.assert_not_called()
        load.assert_not_called()

    def test_active_write_keeps_target_when_planning_switch_is_off(self):
        morning = bms_seite.datetime(2026, 11, 14, 10, 0, tzinfo=bms_seite.TZ)
        settings = self._settings_sockel(12000, sockel=20, target_kw=1.5)
        settings["real_mode"] = True
        settings["planning_hold"] = True
        kw = bms_seite.soll_netzbezug_kw(
            settings, {"soc_percent": 50}, planning_on=False, now=morning
        )
        self.assertEqual(kw, 1.5)

    def test_planning_hold_starts_from_grundverbrauch_when_write_was_off(self):
        settings = {
            "real_mode": False,
            "planning_hold": False,
            "community_hold": False,
            "exchange_hold": False,
            "manual_off": False,
            "soc_stop": False,
            "target_grid_kw": 0.0,
        }

        def remember(updates):
            merged = dict(settings)
            merged.update(updates)
            settings.update(merged)
            return merged

        with mock.patch(
            "energie_steuerung.load_settings", return_value={"algorithmische_planung": True}
        ), mock.patch(
            "grundverbrauch_rechner.recommended_import_kw", return_value=1.5
        ), mock.patch.object(bms_seite, "save_settings", side_effect=remember) as save:
            started = bms_seite.apply_planning_hold(settings, {"soc_percent": 50})
        save.assert_called_once_with(
            {"real_mode": True, "target_grid_kw": 1.5, "planning_hold": True}
        )
        self.assertTrue(started["real_mode"])
        self.assertTrue(started["planning_hold"])
        self.assertEqual(started["target_grid_kw"], 1.5)


class ReleaseLoopAndTrackerTests(unittest.TestCase):
    def tearDown(self):
        bms_seite._release_cooldown_until = 0.0
        bms_seite._last_drift_check = 0.0
        bms_seite._last_drift_release = 0.0
        bms_seite._clear_release_block()

    def test_drift_does_not_restart_a_failed_release(self):
        settings = {"real_mode": False, "release_pending": False, "idle_peak_w": 12000, "install_solar_sell": 0}
        regs = {"solar_sell": "on", "peak_w": "12880"}
        bms_seite._release_block_key = bms_seite._release_settings_key(settings)
        bms_seite._release_block_gap = bms_seite._observed_gap(regs)
        bms_seite._last_drift_check = 0.0
        bms_seite._release_cooldown_until = 0.0
        with mock.patch("bms_seite.read_register_state", return_value=regs):
            self.assertFalse(bms_seite._queue_release_if_drift(settings))
        self.assertFalse(settings["release_pending"])

    def test_three_failures_lock_an_unchanged_release(self):
        bms_seite._clear_release_block()
        bms_seite._release_attempts = 2
        bms_seite._release_sent = False
        bms_seite._needs_release = True
        bms_seite._last_drift_check = 0.0
        settings = {
            "real_mode": False,
            "release_pending": True,
            "inverter": "deye_hybrid",
            "modbus_host": "192.168.1.176",
            "modbus_port": 8899,
            "modbus_unit": 1,
            "known_entry_ids": [],
            "idle_peak_w": 12000,
            "install_solar_sell": 0,
            "idle_work_mode": 2,
            "idle_energy_pattern": 1,
        }
        regs = {"solar_sell": "on", "peak_w": "12880"}
        with mock.patch.object(bms_seite, "resolve_ha_link", return_value=None), mock.patch.object(
            bms_seite, "_send_verified", return_value={"command_sent": False, "write_text": "fehlgeschlagen"}
        ), mock.patch.object(bms_seite, "_read_peak_flags", return_value=1), mock.patch.object(
            bms_seite, "read_register_state", return_value=regs
        ), mock.patch.object(bms_seite, "_persist_release_flag"):
            result = bms_seite.apply_write(settings, {"target_grid_kw": 0})
        self.assertIn("Freigabe abgebrochen", result.get("write_text") or "")
        self.assertFalse(settings["release_pending"])
        self.assertTrue(bms_seite._release_loop_locked(settings, regs))
        bms_seite._last_drift_check = 0.0
        with mock.patch.object(bms_seite, "read_register_state", return_value=regs):
            self.assertFalse(bms_seite._queue_release_if_drift(settings))
        self.assertFalse(settings["release_pending"])

    def test_changed_setpoint_lets_drift_queue_again(self):
        bms_seite._clear_release_block()
        bms_seite._last_drift_check = 0.0
        bms_seite._release_sent = True
        locked = {
            "real_mode": False,
            "release_pending": False,
            "idle_peak_w": 12000,
            "install_solar_sell": 0,
        }
        regs = {"solar_sell": "on", "peak_w": "12880"}
        bms_seite._release_block_key = bms_seite._release_settings_key(locked)
        bms_seite._release_block_gap = bms_seite._observed_gap(regs)
        changed = dict(locked)
        changed["idle_peak_w"] = 8000
        fresh = dict(changed)
        with mock.patch.object(bms_seite, "read_register_state", return_value=regs), mock.patch.object(
            bms_seite, "load_settings", return_value=fresh
        ), mock.patch.object(bms_seite, "_persist_settings"):
            self.assertTrue(bms_seite._queue_release_if_drift(changed))
        self.assertTrue(changed["release_pending"])

    def test_manual_off_clears_hold_flags(self):
        data = bms_seite.default_settings()
        data["real_mode"] = True
        data["manual_off"] = False
        data["planning_hold"] = True
        data["community_hold"] = True
        data["exchange_hold"] = True
        data["modbus_host"] = ""
        with mock.patch.object(bms_seite, "load_settings", return_value=data), mock.patch.object(
            bms_seite, "_persist_settings"
        ), mock.patch.object(bms_seite, "_apply_ha_link", side_effect=lambda item: item):
            saved = bms_seite.save_settings({"real_mode": False, "manual_off": True})
        self.assertTrue(saved["manual_off"])
        self.assertFalse(saved["real_mode"])
        self.assertFalse(saved["planning_hold"])
        self.assertFalse(saved["community_hold"])
        self.assertFalse(saved["exchange_hold"])

    def test_drift_does_not_read_registers_every_tick(self):
        bms_seite._release_cooldown_until = 0.0
        bms_seite._last_drift_check = bms_seite.time.monotonic()
        settings = {"real_mode": False, "release_pending": False}
        with mock.patch("bms_seite.read_register_state") as read:
            self.assertFalse(bms_seite._queue_release_if_drift(settings))
        read.assert_not_called()

    def test_manual_off_keeps_writing_off(self):
        settings = {
            "manual_off": True,
            "real_mode": False,
            "planning_hold": True,
            "community_hold": False,
            "exchange_hold": False,
        }

        def remember(updates):
            merged = dict(settings)
            merged.update(updates)
            return merged

        with mock.patch.object(bms_seite, "save_settings", side_effect=remember) as save:
            result = bms_seite.apply_planning_hold(settings)
        save.assert_called_once_with(
            {
                "real_mode": False,
                "planning_hold": False,
                "community_hold": False,
                "exchange_hold": False,
            }
        )
        self.assertFalse(result["real_mode"])
        self.assertFalse(result["planning_hold"])

    def test_missing_logger_does_not_drop_the_new_ip(self):
        settings = bms_seite.default_settings()
        settings["logger_ip"] = "10.0.0.10"
        settings["modbus_host"] = "10.0.0.10"
        settings["logger_mac"] = "74:e9:d8:3a:a0:fc"
        settings["tracked_inverters"] = [
            {
                "mac": "74:e9:d8:3a:a0:fc",
                "ip": "10.0.0.10",
                "serial": 1,
                "control": True,
                "tracked_at": "alt",
            }
        ]
        with mock.patch.object(bms_seite, "load_settings", return_value=settings), mock.patch.object(
            bms_seite, "discover_loggers", return_value=[]
        ), mock.patch.object(bms_seite, "discover_logger", return_value=None), mock.patch.object(
            bms_seite, "_persist_settings"
        ):
            result = bms_seite.refresh_tracked_inverters()
        self.assertEqual(settings["logger_ip"], "10.0.0.10")
        self.assertEqual(settings["modbus_host"], "10.0.0.10")
        self.assertEqual(settings["tracked_inverters"][0]["tracked_at"], "alt")
        self.assertEqual(result["missing"], ["74:e9:d8:3a:a0:fc"])
        self.assertFalse(result["ok"])

    def test_found_logger_ip_is_written_through(self):
        settings = bms_seite.default_settings()
        settings["logger_ip"] = "192.168.1.176"
        settings["modbus_host"] = "192.168.1.176"
        settings["logger_mac"] = "74:e9:d8:3a:a0:fc"
        settings["logger_serial"] = 5
        settings["tracked_inverters"] = [
            {
                "mac": "74:e9:d8:3a:a0:fc",
                "ip": "192.168.1.176",
                "serial": 5,
                "control": True,
                "tracked_at": "alt",
            }
        ]
        found = {"ip": "10.0.0.10", "mac": "74:e9:d8:3a:a0:fc", "serial": 5}
        with mock.patch.object(bms_seite, "load_settings", return_value=settings), mock.patch.object(
            bms_seite, "discover_loggers", return_value=[found]
        ), mock.patch.object(
            bms_seite, "sync_logger_to_ha", return_value={"ok": True, "message": "ok"}
        ), mock.patch.object(bms_seite, "_persist_settings"):
            result = bms_seite.refresh_tracked_inverters()
        self.assertTrue(result["changed_control"])
        self.assertEqual(settings["logger_ip"], "10.0.0.10")
        self.assertEqual(settings["modbus_host"], "10.0.0.10")
        self.assertEqual(settings["tracked_inverters"][0]["ip"], "10.0.0.10")

    def test_save_keeps_the_old_ip_when_the_mac_is_not_confirmed(self):
        data = bms_seite.default_settings()
        data["modbus_host"] = "192.168.1.176"
        data["logger_ip"] = "192.168.1.176"
        data["logger_mac"] = "d4:27:87:0a:d7:b4"
        data["tracked_inverters"] = [
            {"control": True, "mac": "d4:27:87:0a:d7:b4", "ip": "192.168.1.176", "serial": 1}
        ]
        with mock.patch.object(bms_seite, "load_settings", return_value=data), mock.patch.object(
            bms_seite, "_apply_ha_link", side_effect=lambda item: item
        ), mock.patch.object(bms_seite, "_persist_settings"), mock.patch.object(
            bms_seite, "_confirm_logger_at_ip", return_value=None
        ), mock.patch.object(bms_seite, "sync_logger_to_ha") as sync, mock.patch.object(
            bms_seite, "track_logger", return_value={"ok": False}
        ) as track:
            saved = bms_seite.save_settings({"modbus_host": "10.0.0.10"})
        self.assertEqual(saved["modbus_host"], "192.168.1.176")
        self.assertEqual(saved["logger_mac"], "d4:27:87:0a:d7:b4")
        self.assertEqual(data["tracked_inverters"][0]["ip"], "192.168.1.176")
        sync.assert_not_called()
        track.assert_not_called()

    def test_confirm_uses_probe_or_arp_at_the_entered_ip(self):
        probed = {"ip": "10.0.0.10", "mac": "74:e9:d8:3a:a0:fc", "serial": 5, "port": 8899}
        with mock.patch.object(bms_seite, "_unicast_discover", return_value=probed), mock.patch.object(
            bms_seite, "_arp_mac_for_ip", return_value="74:e9:d8:3a:a0:fc"
        ):
            found = bms_seite._confirm_logger_at_ip("10.0.0.10")
        self.assertEqual(found["mac"], "74:e9:d8:3a:a0:fc")
        self.assertEqual(found["ip"], "10.0.0.10")
        with mock.patch.object(bms_seite, "_unicast_discover", return_value=None), mock.patch.object(
            bms_seite, "_arp_mac_for_ip", return_value="74:e9:d8:3a:a0:fc"
        ):
            found = bms_seite._confirm_logger_at_ip("10.0.0.10")
        self.assertEqual(found["mac"], "74:e9:d8:3a:a0:fc")
        with mock.patch.object(bms_seite, "_unicast_discover", return_value=probed), mock.patch.object(
            bms_seite, "_arp_mac_for_ip", return_value="d4:27:87:0a:d7:b4"
        ):
            self.assertIsNone(bms_seite._confirm_logger_at_ip("10.0.0.10"))
        with mock.patch.object(bms_seite, "_unicast_discover", return_value=None), mock.patch.object(
            bms_seite, "_arp_mac_for_ip", return_value=""
        ):
            self.assertIsNone(bms_seite._confirm_logger_at_ip("10.0.0.10"))

    def test_arp_lookup_reads_the_mac_for_that_ip_only(self):
        text = (
            "IP address       HW type     Flags       HW address            Mask     Device\n"
            "192.168.1.176    0x1         0x2         d4:27:87:0a:d7:b4     *        eth0\n"
            "10.0.0.10        0x1         0x0         00:00:00:00:00:00     *        eth0\n"
        )
        with mock.patch.object(bms_seite.Path, "read_text", return_value=text):
            self.assertEqual(bms_seite._arp_mac_for_ip("192.168.1.176"), "d4:27:87:0a:d7:b4")
            self.assertEqual(bms_seite._arp_mac_for_ip("10.0.0.10"), "")
            self.assertEqual(bms_seite._arp_mac_for_ip("10.0.0.11"), "")

    def test_entered_ip_is_stored_only_after_mac_and_ha_agree(self):
        settings = bms_seite.default_settings()
        settings["modbus_host"] = "192.168.1.176"
        settings["logger_ip"] = "192.168.1.176"
        settings["logger_mac"] = "d4:27:87:0a:d7:b4"
        settings["tracked_inverters"] = [
            {"control": True, "mac": "d4:27:87:0a:d7:b4", "ip": "192.168.1.176", "serial": 1}
        ]
        found = {"ip": "10.0.0.10", "mac": "74:e9:d8:3a:a0:fc", "serial": 5, "port": 8899}
        with mock.patch.object(bms_seite, "load_settings", return_value=settings), mock.patch.object(
            bms_seite, "_confirm_logger_at_ip", return_value=None
        ), mock.patch.object(bms_seite, "_persist_settings") as persist:
            refused = bms_seite.add_tracked_inverter("", "Wechselrichter", 0, host="10.0.0.10")
        self.assertFalse(refused["ok"])
        persist.assert_not_called()
        self.assertEqual(settings["logger_mac"], "d4:27:87:0a:d7:b4")
        self.assertEqual(settings["modbus_host"], "192.168.1.176")

        with mock.patch.object(bms_seite, "load_settings", return_value=settings), mock.patch.object(
            bms_seite, "_confirm_logger_at_ip", return_value=found
        ), mock.patch.object(
            bms_seite, "sync_logger_to_ha", return_value={"ok": False, "message": "kein Eintrag"}
        ) as sync, mock.patch.object(bms_seite, "_persist_settings") as persist:
            blocked = bms_seite.add_tracked_inverter("", "Wechselrichter", 0, host="10.0.0.10")
        self.assertFalse(blocked["ok"])
        persist.assert_not_called()
        sync.assert_called_once_with("10.0.0.10", "74:e9:d8:3a:a0:fc", 5)
        self.assertEqual(settings["modbus_host"], "192.168.1.176")

        with mock.patch.object(bms_seite, "load_settings", return_value=settings), mock.patch.object(
            bms_seite, "_confirm_logger_at_ip", return_value=found
        ), mock.patch.object(
            bms_seite, "sync_logger_to_ha", return_value={"ok": True, "message": "aktualisiert"}
        ) as sync, mock.patch.object(bms_seite, "_persist_settings") as persist:
            saved = bms_seite.add_tracked_inverter("", "Wechselrichter", 0, host="10.0.0.10")
        self.assertTrue(saved["ok"])
        persist.assert_called_once()
        sync.assert_called_once_with("10.0.0.10", "74:e9:d8:3a:a0:fc", 5)
        self.assertEqual(settings["modbus_host"], "10.0.0.10")
        self.assertEqual(settings["logger_ip"], "10.0.0.10")
        self.assertEqual(settings["logger_mac"], "74:e9:d8:3a:a0:fc")
        self.assertEqual(settings["tracked_inverters"][0]["ip"], "10.0.0.10")
        self.assertEqual(settings["tracked_inverters"][0]["mac"], "74:e9:d8:3a:a0:fc")


class LoggerResolveTests(unittest.TestCase):
    def test_publisher_does_not_scan(self):
        settings = bms_seite.default_settings()
        settings["modbus_host"] = "192.168.1.176"
        settings["logger_ip"] = "192.168.1.176"
        with mock.patch.object(bms_seite, "load_settings", return_value=settings), mock.patch.object(
            bms_seite, "_updates_are_published_here", return_value=True
        ), mock.patch.object(bms_seite, "discover_loggers") as scan:
            result = bms_seite.resolve_logger_address()
        scan.assert_not_called()
        self.assertTrue(result["skipped"])
        self.assertEqual(result["ip"], "192.168.1.176")

    def test_single_local_logger_is_written(self):
        settings = bms_seite.default_settings()
        settings["modbus_host"] = ""
        settings["logger_ip"] = ""
        settings["logger_mac"] = ""
        settings["tracked_inverters"] = []
        found = {"ip": "192.168.1.50", "mac": "aa:bb:cc:dd:ee:ff", "serial": 42}
        with mock.patch.object(bms_seite, "load_settings", return_value=settings), mock.patch.object(
            bms_seite, "_updates_are_published_here", return_value=False
        ), mock.patch.object(bms_seite, "discover_loggers", return_value=[found]), mock.patch.object(
            bms_seite, "sync_logger_to_ha", return_value={"ok": False, "message": "kein Eintrag"}
        ), mock.patch.object(bms_seite, "_persist_settings") as persist:
            result = bms_seite.resolve_logger_address()
        self.assertTrue(result["ok"])
        self.assertEqual(settings["modbus_host"], "192.168.1.50")
        self.assertEqual(settings["logger_ip"], "192.168.1.50")
        self.assertEqual(settings["logger_last_known_ip"], "192.168.1.50")
        self.assertEqual(settings["logger_mac"], "aa:bb:cc:dd:ee:ff")
        persist.assert_called_once()

    def test_several_loggers_keep_the_last_address(self):
        settings = bms_seite.default_settings()
        settings["modbus_host"] = "192.168.1.20"
        settings["logger_ip"] = "192.168.1.20"
        settings["logger_last_known_ip"] = "192.168.1.20"
        settings["logger_mac"] = ""
        items = [
            {"ip": "192.168.1.21", "mac": "aa:bb:cc:dd:ee:01", "serial": 1},
            {"ip": "192.168.1.22", "mac": "aa:bb:cc:dd:ee:02", "serial": 2},
        ]
        with mock.patch.object(bms_seite, "load_settings", return_value=settings), mock.patch.object(
            bms_seite, "_updates_are_published_here", return_value=False
        ), mock.patch.object(bms_seite, "_confirm_logger_at_ip", return_value=None), mock.patch.object(
            bms_seite, "discover_loggers", return_value=items
        ), mock.patch.object(bms_seite, "_persist_settings") as persist:
            result = bms_seite.resolve_logger_address()
        self.assertFalse(result["ok"])
        self.assertEqual(result["ip"], "192.168.1.20")
        self.assertEqual(settings["modbus_host"], "192.168.1.20")
        persist.assert_not_called()
        self.assertTrue(any("Mehrere Logger" in line for line in bms_seite._logger_resolve_messages))

    def test_search_error_keeps_the_last_address_and_does_not_raise(self):
        settings = bms_seite.default_settings()
        settings["modbus_host"] = "192.168.1.20"
        settings["logger_ip"] = "192.168.1.20"
        settings["logger_last_known_ip"] = "192.168.1.20"
        with mock.patch.object(bms_seite, "load_settings", return_value=settings), mock.patch.object(
            bms_seite, "_updates_are_published_here", return_value=False
        ), mock.patch.object(bms_seite, "_confirm_logger_at_ip", side_effect=OSError("netz weg")), mock.patch.object(
            bms_seite, "discover_loggers", side_effect=OSError("broadcast tot")
        ), mock.patch.object(bms_seite, "_persist_settings") as persist:
            result = bms_seite.resolve_logger_address()
        self.assertFalse(result["ok"])
        self.assertEqual(result["ip"], "192.168.1.20")
        self.assertEqual(settings["modbus_host"], "192.168.1.20")
        persist.assert_not_called()

    def test_answering_host_is_not_replaced(self):
        settings = bms_seite.default_settings()
        settings["modbus_host"] = "192.168.1.20"
        settings["logger_ip"] = "192.168.1.20"
        settings["logger_mac"] = "aa:bb:cc:dd:ee:ff"
        settings["logger_serial"] = 7
        settings["logger_last_known_ip"] = "192.168.1.20"
        confirmed = {"ip": "192.168.1.20", "mac": "aa:bb:cc:dd:ee:ff", "serial": 7}
        with mock.patch.object(bms_seite, "load_settings", return_value=settings), mock.patch.object(
            bms_seite, "_updates_are_published_here", return_value=False
        ), mock.patch.object(bms_seite, "_confirm_logger_at_ip", return_value=confirmed), mock.patch.object(
            bms_seite, "discover_loggers"
        ) as scan:
            result = bms_seite.resolve_logger_address()
        scan.assert_not_called()
        self.assertTrue(result["ok"])
        self.assertFalse(result["changed"])
        self.assertEqual(settings["modbus_host"], "192.168.1.20")


if __name__ == "__main__":
    unittest.main()
