# -*- coding: utf-8 -*-
import json
import unittest
from unittest.mock import patch

from energiegemeinschaft_mesh import (
    accrue_connected_kwh,
    append_history,
    history_for_display,
    code_is_valid,
    community_price_key,
    format_stillzeit,
    price_comparison,
    command_packet,
    feed_packet,
    generate_invite_code,
    merge_invite_codes,
    balance_lamp,
    night_balance_kwh_from_state,
    outlook_line,
    verify_any,
    handle_datagram,
    is_community_counterpart,
    verify_packet,
)


class TestEnergiegemeinschaftMesh(unittest.TestCase):
    def test_stillzeit_is_human_readable(self):
        self.assertEqual(format_stillzeit(None), "noch keine Meldung")
        self.assertEqual(format_stillzeit(30), "letzte Minute da")
        self.assertEqual(format_stillzeit(120), "still seit 2 Min.")
        self.assertEqual(format_stillzeit(90427), "still seit 1 Tag 1 Std.")
        self.assertEqual(format_stillzeit(417752), "still seit 4 Tagen 20 Std.")
        self.assertEqual(format_stillzeit(100, code_mismatch=True), "im Netz, der Code passt nicht mehr")

    def test_code_format(self):
        code = generate_invite_code()
        self.assertTrue(code_is_valid(code))
        self.assertTrue(code.startswith("EG-"))
        self.assertEqual(len(code), 22)
        self.assertTrue(code_is_valid("EG-7K2M-Q4HP"))
        self.assertTrue(code_is_valid("EG-7K2M-Q4HP-AB23-ZZ99"))
        self.assertFalse(code_is_valid("EG-7K2M-Q4HP-AB23"))
        self.assertEqual(len({generate_invite_code() for _ in range(2000)}), 2000)

    def test_cloned_image_gets_own_code_existing_device_keeps_it(self):
        import tempfile
        from pathlib import Path

        import energiegemeinschaft_mesh as mesh

        path = Path(tempfile.mkstemp(suffix=".json")[1])
        try:
            with patch.object(mesh, "SETTINGS_FILE", path):
                # running device from before the update: code is adopted, not replaced
                path.write_text(json.dumps({"invite_code": "EG-AAAA-BBBB", "node_name": "a", "members": {}}), encoding="utf-8")
                with patch.object(mesh, "device_fingerprint", return_value="pi-a"):
                    self.assertFalse(mesh.ensure_device_code())
                    self.assertFalse(mesh.ensure_device_code())
                stored = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual((stored["invite_code"], stored["code_device"]), ("EG-AAAA-BBBB", "pi-a"))
                # same SD card image on other hardware: fresh code, old codes dropped
                stored["invite_codes"] = ["EG-CCCC-DDDD"]
                path.write_text(json.dumps(stored), encoding="utf-8")
                with patch.object(mesh, "device_fingerprint", return_value="pi-b"):
                    self.assertTrue(mesh.ensure_device_code())
                stored = json.loads(path.read_text(encoding="utf-8"))
                self.assertNotEqual(stored["invite_code"], "EG-AAAA-BBBB")
                self.assertEqual(len(stored["invite_code"]), 22)
                self.assertEqual(stored["invite_codes"], [])
                self.assertEqual(stored["node_name"], "")
                self.assertEqual(mesh.accepted_codes(), [stored["invite_code"]])
        finally:
            path.unlink(missing_ok=True)

    def test_feed_packet_carries_export_and_soc_and_checks_out(self):
        code = "EG-7K2M-Q4HP"
        packet = feed_packet("haus-nord", 1.25, 71, code)
        self.assertEqual(packet["feed_in_kw"], 1.25)
        self.assertEqual(packet["soc_percent"], 71.0)
        with_energy = feed_packet("haus-nord", 1.25, 71, code, 8.2)
        self.assertEqual(with_energy["feed_in_kwh"], 8.2)
        self.assertTrue(verify_packet(packet, code))
        self.assertFalse(verify_packet(packet, "EG-AAAA-AAAA"))

    def test_command_is_addressed_to_one_pi(self):
        code = "EG-7K2M-Q4HP"
        packet = command_packet("piminer", "haus-nord", "heute Abend drosseln", code)
        self.assertEqual(packet["kind"], "cmd")
        self.assertEqual(packet["to"], "haus-nord")
        self.assertNotEqual(packet["to"], "piminer")
        self.assertTrue(verify_packet(packet, code))

    def test_second_code_keeps_the_first_code_valid(self):
        first = "EG-7K2M-Q4HP"
        second = "EG-AAAA-BBBB"
        kept = merge_invite_codes(first, [], second)
        self.assertEqual(kept, [first])
        packet = feed_packet("haus-martin", 2.9, 100, first)
        self.assertTrue(verify_any(packet, [second, *kept]))
        self.assertFalse(verify_packet(packet, second))

    def test_previous_codes_keep_fifty_versions(self):
        from energiegemeinschaft_mesh import _ALPHABET

        codes = [f"EG-AA{_ALPHABET[i // 32]}{_ALPHABET[i % 32]}-AAAA" for i in range(51)]
        kept = merge_invite_codes(codes[0], codes[1:], "EG-ZZZZ-ZZZZ")
        self.assertEqual(len(kept), 50)
        self.assertNotIn("EG-ZZZZ-ZZZZ", kept)

    def test_known_house_stays_online_after_the_code_is_replaced(self):
        packet = feed_packet("raspberrypi2-1", 2.9, 88, "EG-7K2M-Q4HP")
        raw = json.dumps(packet).encode("utf-8")
        settings = {
            "invite_code": "EG-AAAA-BBBB",
            "members": {
                "raspberrypi2-1": {"name": "raspberrypi2-1", "ip": "100.9.9.9", "seen": 10},
            },
        }
        with patch("energiegemeinschaft_mesh.mesh_settings", return_value=settings):
            with patch("energiegemeinschaft_mesh._remember_member") as remember:
                handle_datagram(raw, "100.9.9.9")
                remember.assert_called_once()
                remember.reset_mock()
                handle_datagram(raw, "100.8.8.8")
                remember.assert_not_called()

    def test_listener_drops_packet_without_valid_code(self):
        packet = feed_packet("fremd", 0.4, 50, "EG-7K2M-Q4HP")
        raw = json.dumps(packet).encode("utf-8")
        with patch("energiegemeinschaft_mesh.mesh_settings", return_value={"invite_code": "EG-AAAA-BBBB"}):
            with patch("energiegemeinschaft_mesh._remember_member") as remember:
                handle_datagram(raw, "100.1.1.1")
        remember.assert_not_called()

    def test_connected_energy_counts_only_while_linked(self):
        self.assertEqual(accrue_connected_kwh(0, 0, 1, 3600, 1), 0)
        self.assertEqual(accrue_connected_kwh(0, 1000, 2, 1060, 2), 0.033)
        self.assertEqual(accrue_connected_kwh(1, 1000, 4, 1000 + 1000, 4), 1)

    def test_each_device_keeps_its_own_history(self):
        martin = append_history([], 100, 1.2, 70)
        other = append_history([], 100, 0.4, 40)
        martin = append_history(martin, 160, 1.5, 71)
        self.assertEqual(len(martin), 2)
        self.assertEqual(martin[-1]["feed_in_kw"], 1.5)
        self.assertEqual(martin[-1]["soc_percent"], 71)
        self.assertEqual(len(other), 1)
        self.assertEqual(other[0]["soc_percent"], 40)
        # Ein Folgewert ohne Speicherstand darf den eben geschriebenen Stand nicht löschen.
        kept_soc = append_history(martin, 170, 1.6, None)
        self.assertEqual(len(kept_soc), 2)
        self.assertEqual(kept_soc[-1]["feed_in_kw"], 1.6)
        self.assertEqual(kept_soc[-1]["soc_percent"], 71)

    def test_current_reading_is_the_first_point_immediately(self):
        item = {"feed_in_kw": 2.4, "soc_percent": 81, "seen": 1000, "history": []}
        points = history_for_display(item, 1060)
        self.assertEqual(len(points), 1)
        self.assertEqual(points[0]["ts"], 1000)
        self.assertEqual(points[0]["feed_in_kw"], 2.4)
        self.assertEqual(points[0]["soc_percent"], 81.0)
        kept = history_for_display(
            {"feed_in_kw": 2.4, "soc_percent": 81, "seen": 1000, "history": points},
            1060,
        )
        self.assertEqual(len(kept), 1)

    def test_cheapest_price_comes_first_and_costs_add_up(self):
        rows = [
            {"name": "teuer", "ask_ct_kwh": 12},
            {"name": "ohne", "ask_ct_kwh": None},
            {"name": "guenstig", "ask_ct_kwh": 6},
        ]
        rows.sort(key=community_price_key)
        self.assertEqual([row["name"] for row in rows], ["guenstig", "teuer", "ohne"])
        compared = price_comparison(12, 6)
        self.assertEqual(compared["total_ct"], 18)
        self.assertEqual(compared["delta_ct"], -6)
        self.assertFalse(price_comparison(12, None)["ready"])

    def test_only_the_counterpart_with_the_code_is_visible(self):
        self.assertFalse(is_community_counterpart({"name": "ich", "seen": 5, "ip": "100.0.0.1"}, "ich", "100.0.0.1"))
        self.assertFalse(is_community_counterpart({"name": "Ich", "seen": 5, "ip": "100.0.0.1"}, "ich", "100.0.0.1"))
        self.assertFalse(is_community_counterpart({"name": "konto", "seen": 0, "ip": "100.0.0.2"}, "ich", "100.0.0.1"))
        self.assertTrue(is_community_counterpart({"name": "pi-2-1", "seen": 10, "ip": "100.0.0.9"}, "ich", "100.0.0.1"))

    def test_restart_updates_the_same_device_instead_of_adding_one(self):
        import tempfile
        from pathlib import Path

        import energiegemeinschaft_mesh as mesh

        path = Path(tempfile.mkstemp(suffix=".json")[1])
        path.write_text("{}", encoding="utf-8")
        try:
            with patch.object(mesh, "SETTINGS_FILE", path):
                mesh._remember_member("raspberrypi2-1", "100.91.87.103", 1.2, 70, 4.5)
                mesh.update_member_field("raspberrypi2-1", "alias", "Haus Martin")
                mesh._remember_member("piminer", "100.105.158.95", 0.0, 70, None)
                mesh._remember_member("raspberrypi", "100.91.87.103", 1.4, 71, 4.6)
                mesh._remember_member("piminer", "100.105.158.95", 0.1, 69, None)
                stored = json.loads(path.read_text(encoding="utf-8"))["members"]
                self.assertEqual({key.casefold() for key in stored}, {"raspberrypi2-1", "piminer"})
                self.assertEqual(stored["raspberrypi2-1"]["alias"], "Haus Martin")
                self.assertEqual(stored["raspberrypi2-1"]["ip"], "100.91.87.103")
                self.assertEqual(stored["raspberrypi2-1"]["feed_in_kw"], 1.4)
                self_key = next(key for key in stored if key.casefold() == "piminer")
                self.assertEqual(stored[self_key]["ip"], "100.105.158.95")
                self.assertEqual(stored[self_key]["feed_in_kw"], 0.1)
                mesh.ensure_node_name("piminer")
                mesh.ensure_node_name("anderer-name")
                self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["node_name"], "piminer")
        finally:
            path.unlink(missing_ok=True)

    def test_display_name_is_used_as_local_sender(self):
        import tempfile
        from pathlib import Path

        import energiegemeinschaft_mesh as mesh

        path = Path(tempfile.mkstemp(suffix=".json")[1])
        path.write_text(json.dumps({"node_name": "piminer-4"}), encoding="utf-8")
        try:
            with patch.object(mesh, "SETTINGS_FILE", path):
                self.assertEqual(mesh.local_name({"self_name": "tail-host"}), "piminer-4")
                self.assertEqual(mesh.set_display_name("  Haus Martin  "), "Haus Martin")
                self.assertEqual(mesh.local_name({"self_name": "tail-host"}), "Haus Martin")
                self.assertEqual(mesh.mesh_settings()["display_name"], "Haus Martin")
                self.assertEqual(mesh.set_display_name(""), "")
                self.assertEqual(mesh.local_name({"self_name": "tail-host"}), "piminer-4")
                with self.assertRaises(ValueError):
                    mesh.set_display_name("x" * 65)
        finally:
            path.unlink(missing_ok=True)

    def test_name_price_and_grid_cost_survive_the_next_minute(self):
        import tempfile
        from pathlib import Path

        import energiegemeinschaft_mesh as mesh

        path = Path(tempfile.mkstemp(suffix=".json")[1])
        path.write_text("{}", encoding="utf-8")
        try:
            with patch.object(mesh, "SETTINGS_FILE", path), patch.object(mesh, "local_name", return_value="hier"):
                mesh.update_member_field("pi-2-1", "alias", "Haus Martin")
                mesh.update_member_field("pi-2-1", "ask_ct_kwh", "12,5")
                mesh.update_member_field("pi-2-1", "grid_cost_ct_kwh", "3")
                mesh._remember_member("pi-2-1", "100.1.1.2", 1.2, 70, 4.5)
                stored = json.loads(path.read_text(encoding="utf-8"))["members"]["pi-2-1"]
                self.assertEqual(stored["alias"], "Haus Martin")
                self.assertEqual(stored["ask_ct_kwh"], 12.5)
                self.assertEqual(stored["grid_cost_ct_kwh"], 3.0)
                self.assertEqual(stored["feed_in_kwh"], 4.5)
                with self.assertRaises(ValueError):
                    mesh.update_member_field("hier", "grid_cost_ct_kwh", "1")
        finally:
            path.unlink(missing_ok=True)

    def test_removed_member_cannot_rejoin(self):
        import tempfile
        from pathlib import Path

        import energiegemeinschaft_mesh as mesh

        path = Path(tempfile.mkstemp(suffix=".json")[1])
        path.write_text(json.dumps({"invite_code": "EG-AAAA-BBBB", "members": {}}), encoding="utf-8")
        try:
            with patch.object(mesh, "SETTINGS_FILE", path), patch.object(mesh, "local_name", return_value="hier"):
                mesh._remember_member("pi-2-1", "100.1.1.2", 1.2, 70, 4.5)
                mesh._remember_member("hier", "100.1.1.1", 0.1, 50, None)
                mesh.remove_member("pi-2-1")
                stored = json.loads(path.read_text(encoding="utf-8"))
                self.assertNotIn("pi-2-1", stored["members"])
                self.assertIn("hier", stored["members"])
                self.assertEqual(stored["invite_code"], "EG-AAAA-BBBB")
                self.assertIn("pi-2-1", stored["removed_members"])
                packet = feed_packet("pi-2-1", 1.0, 40, "EG-AAAA-BBBB")
                mesh.handle_datagram(json.dumps(packet).encode("utf-8"), "100.1.1.2")
                stored = json.loads(path.read_text(encoding="utf-8"))
                self.assertNotIn("pi-2-1", stored["members"])
                with self.assertRaises(ValueError):
                    mesh.remove_member("unbekannt")
                with self.assertRaises(ValueError):
                    mesh.remove_member("hier")
        finally:
            path.unlink(missing_ok=True)

    def test_feed_packet_carries_stored_day_balance(self):
        code = "EG-7K2M-Q4HP"
        packet = feed_packet("haus-nord", 1.25, 71, code, 8.2, -3.66)
        self.assertEqual(packet["balance_kwh"], -3.66)
        self.assertTrue(verify_packet(packet, code))
        without = feed_packet("haus-nord", 1.25, 71, code, 8.2)
        self.assertNotIn("balance_kwh", without)

    def test_night_balance_is_the_stored_day_balance(self):
        state = {
            "energy_budget_snapshots": [
                {
                    "production_daily": {"total_kwh": 10.7},
                    "day_balance": {
                        "production_kwh": 10.7,
                        "consumption_kwh": 14.36,
                        "balance_kwh": 10.69,
                    },
                }
            ]
        }
        self.assertEqual(night_balance_kwh_from_state(state), 10.69)
        self.assertIsNone(night_balance_kwh_from_state({}))
        self.assertIsNone(night_balance_kwh_from_state({"energy_budget_snapshots": [{}]}))

    def test_outlook_uses_the_stored_balance_and_its_unit(self):
        self.assertEqual(
            outlook_line(None),
            "Nachbar hat noch keinen Wert gemeldet.",
        )
        self.assertEqual(
            outlook_line(-3.66),
            "Nachbar wird nicht über die Nacht kommen, er würde 3,66 kWh benötigen.",
        )
        self.assertEqual(
            outlook_line(0),
            "Nachbar kommt über die Nacht, die Bilanz ist 0 kWh.",
        )
        self.assertEqual(
            outlook_line(10.69),
            "Nachbar kommt über die Nacht, die Bilanz ist +10,69 kWh.",
        )
        self.assertNotIn("3 kW", outlook_line(None))

    def test_balance_lamp_uses_one_kwh_band(self):
        self.assertIsNone(balance_lamp(None))
        self.assertIsNone(balance_lamp("fehlt"))
        green = balance_lamp(1.01)
        self.assertEqual(green["level"], "green")
        self.assertFalse(green["fill"])
        self.assertEqual(balance_lamp(10.69)["level"], "green")
        near = balance_lamp(1)
        self.assertEqual(near["level"], "light-green")
        self.assertFalse(near["fill"])
        self.assertEqual(balance_lamp(0)["level"], "light-green")
        self.assertEqual(balance_lamp(-1)["level"], "light-green")
        short = balance_lamp(-1.01)
        self.assertEqual(short["level"], "short")
        self.assertTrue(short["fill"])
        self.assertTrue(balance_lamp(-3.66)["fill"])
        self.assertNotEqual(balance_lamp(0)["level"], "orange")

    def test_admin_settings_stay_on_one_member(self):
        import tempfile
        from pathlib import Path

        import energiegemeinschaft_mesh as mesh

        path = Path(tempfile.mkstemp(suffix=".json")[1])
        path.write_text("{}", encoding="utf-8")
        try:
            with patch.object(mesh, "SETTINGS_FILE", path), patch.object(mesh, "local_name", return_value="hier"):
                mesh._remember_member("raspberrypi2-1", "100.1.1.2", 0.1, 20, 1, 8.97)
                mesh._remember_member("raspberrypi2", "100.1.1.3", 0.2, 30, 1, 14.73)
                mesh.save_member_admin("raspberrypi2", {
                    "neighbor_trade": "sell",
                    "max_zukauf_kw": "2,5",
                    "neighbor_night_kw": "4",
                    "neighbor_storage": "yes",
                    "neighbor_pv": "no",
                    "share_without_production": "1",
                    "only_if_enough_tomorrow": "0",
                })
                mesh.save_member_admin("raspberrypi2-1", {"neighbor_trade": "buy"})
                mesh._remember_member("raspberrypi2", "100.1.1.3", 0.3, 31, 1, 14.73)
                snap = {"ok": True, "error": "", "self_ip": "100.9.9.9", "self_name": "hier", "peers": []}
                with patch.object(mesh, "tailscale_snapshot", return_value=snap):
                    status = mesh.public_status()
            by_name = {row["name"]: row for row in status["members"]}
            ludwig = by_name["raspberrypi2"]
            martin = by_name["raspberrypi2-1"]
            self.assertEqual(ludwig["admin"]["neighbor_trade"], "sell")
            self.assertEqual(ludwig["admin"]["max_zukauf_kw"], 2.5)
            self.assertEqual(ludwig["admin"]["neighbor_night_kw"], 4.0)
            self.assertTrue(ludwig["admin"]["share_without_production"])
            self.assertFalse(ludwig["admin"]["only_if_enough_tomorrow"])
            self.assertEqual(ludwig["admin"]["neighbor_storage"], "yes")
            self.assertEqual(martin["admin"]["neighbor_trade"], "buy")
            self.assertIsNone(martin["admin"]["max_zukauf_kw"])
            self.assertIsNone(martin["admin"]["neighbor_night_kw"])
            self.assertIsNone(ludwig["reported"]["neighbor_trade"])
            self.assertIsNone(ludwig["reported"]["neighbor_night_kw"] if "neighbor_night_kw" in ludwig["reported"] else None)
            self.assertEqual(ludwig["reported"]["balance_kwh"], 14.73)
            self.assertEqual(ludwig["outlook_lamp"]["level"], "green")
            self.assertFalse(ludwig["outlook_lamp"]["fill"])
            self.assertNotEqual(ludwig["admin"]["neighbor_night_kw"], ludwig["balance_kwh"])
        finally:
            path.unlink(missing_ok=True)

    def test_balance_survives_the_next_packet_and_reaches_the_card(self):
        import tempfile
        from pathlib import Path

        import energiegemeinschaft_mesh as mesh

        path = Path(tempfile.mkstemp(suffix=".json")[1])
        path.write_text("{}", encoding="utf-8")
        try:
            with patch.object(mesh, "SETTINGS_FILE", path), patch.object(mesh, "local_name", return_value="hier"):
                mesh._remember_member("pi-2-1", "100.1.1.2", 1.2, 70, 4.5, -3.66)
                mesh._remember_member("pi-2-1", "100.1.1.2", 1.3, 71, 4.6)
                stored = json.loads(path.read_text(encoding="utf-8"))["members"]["pi-2-1"]
                self.assertEqual(stored["balance_kwh"], -3.66)
                self.assertEqual(stored["feed_in_kw"], 1.3)
                snap = {
                    "ok": True,
                    "error": "",
                    "self_ip": "100.1.1.1",
                    "self_name": "hier",
                    "peers": [],
                }
                with patch.object(mesh, "tailscale_snapshot", return_value=snap):
                    status = mesh.public_status()
                member = status["members"][0]
                self.assertEqual(member["name"], "pi-2-1")
                self.assertEqual(member["balance_kwh"], -3.66)
                self.assertEqual(
                    member["outlook_text"],
                    "Nachbar wird nicht über die Nacht kommen, er würde 3,66 kWh benötigen.",
                )
                mesh._remember_member("pi-2-1", "100.1.1.2", 1.4, 72, 4.7, 0)
                stored = json.loads(path.read_text(encoding="utf-8"))["members"]["pi-2-1"]
                self.assertEqual(stored["balance_kwh"], 0)
        finally:
            path.unlink(missing_ok=True)


class TestSettingsDoNotFlickerBlank(unittest.TestCase):
    def _path(self):
        import tempfile
        from pathlib import Path

        return Path(tempfile.mkstemp(suffix=".json")[1])

    def test_half_written_file_still_shows_code_and_members(self):
        import energiegemeinschaft_mesh as mesh

        path = self._path()
        intact = {
            "invite_code": "EG-AAAA-BBBB",
            "members": {
                "raspberrypi2": {
                    "name": "raspberrypi2",
                    "alias": "Ludwig",
                    "ip": "100.98.134.23",
                }
            },
        }
        path.write_text(json.dumps(intact), encoding="utf-8")
        torn = '{"invite_code": "EG-AAAA-BBBB", "members": {"raspberrypi2": '
        try:
            with patch.object(mesh, "SETTINGS_FILE", path):
                mesh._last_good_path = ""
                mesh._last_good = {}
                seen = mesh.mesh_settings()
                self.assertTrue(seen["invite_code"])
                self.assertEqual(seen["members"]["raspberrypi2"]["alias"], "Ludwig")
                path.write_text(torn, encoding="utf-8")
                seen = mesh.mesh_settings()
                self.assertEqual(seen["invite_code"], "EG-AAAA-BBBB")
                self.assertEqual(seen["members"]["raspberrypi2"]["alias"], "Ludwig")
                mesh._remember_member("raspberrypi2-1", "100.91.87.103", 1.1, 40, 2.0)
                stored = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(stored["invite_code"], "EG-AAAA-BBBB")
                self.assertEqual(stored["members"]["raspberrypi2"]["alias"], "Ludwig")
                self.assertIn("raspberrypi2-1", stored["members"])
        finally:
            path.unlink(missing_ok=True)
            path.with_name(path.name + ".tmp").unlink(missing_ok=True)

    def test_unreadable_file_without_a_prior_read_is_left_alone(self):
        import energiegemeinschaft_mesh as mesh

        path = self._path()
        torn = '{"invite_code": "EG-AAAA-BBBB", "members": {"raspberrypi2": '
        path.write_text(torn, encoding="utf-8")
        try:
            with patch.object(mesh, "SETTINGS_FILE", path):
                mesh._last_good_path = ""
                mesh._last_good = {}
                mesh._remember_member("raspberrypi2-1", "100.91.87.103", 1.1, 40, 2.0)
                self.assertEqual(path.read_text(encoding="utf-8"), torn)
                self.assertEqual(mesh.mesh_settings()["invite_code"], "")
        finally:
            path.unlink(missing_ok=True)

    def test_parallel_read_never_sees_a_partial_community_file(self):
        import threading
        import time

        import energiegemeinschaft_mesh as mesh

        path = self._path()
        path.write_text(
            json.dumps({"invite_code": "EG-AAAA-BBBB", "members": {}}),
            encoding="utf-8",
        )
        errors = []
        stop = threading.Event()

        def writer():
            while not stop.is_set():
                mesh._remember_member("raspberrypi2", "100.98.134.23", 0.4, 55, 1.0)

        try:
            with patch.object(mesh, "SETTINGS_FILE", path):
                mesh._last_good_path = ""
                mesh._last_good = {}
                thread = threading.Thread(target=writer)
                thread.start()
                started = time.time()
                while time.time() - started < 0.35:
                    try:
                        data = json.loads(path.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError) as exc:
                        errors.append(type(exc).__name__)
                        continue
                    if not data.get("invite_code") or not isinstance(data.get("members"), dict):
                        errors.append("blank")
                stop.set()
                thread.join(timeout=2)
            self.assertEqual(errors, [])
            stored = json.loads(path.read_text(encoding="utf-8"))
            self.assertTrue(stored["invite_code"])
            self.assertIn("raspberrypi2", stored["members"])
        finally:
            path.unlink(missing_ok=True)
            path.with_name(path.name + ".tmp").unlink(missing_ok=True)

    def test_rewrite_keeps_history_and_today_feed_when_a_packet_omits_them(self):
        import energiegemeinschaft_mesh as mesh

        path = self._path()
        history = [
            {"ts": 1000 + index * 30, "feed_in_kw": 0.2, "soc_percent": 40 + index}
            for index in range(5)
        ]
        path.write_text(
            json.dumps(
                {
                    "invite_code": "EG-AAAA-BBBB",
                    "members": {
                        "raspberrypi2-1": {
                            "name": "raspberrypi2-1",
                            "alias": "Haus Martin",
                            "ip": "100.91.87.103",
                            "feed_in_kwh": 1.25,
                            "connected_kwh": 4.5,
                            "soc_percent": 44,
                            "balance_kwh": 8.97,
                            "seen": 1120,
                            "history": history,
                        },
                        "raspberrypi2": {
                            "name": "raspberrypi2",
                            "alias": "Ludwig",
                            "ip": "100.98.134.23",
                            "feed_in_kwh": 2.5,
                            "connected_kwh": 9,
                            "history": [{"ts": 1100, "feed_in_kw": 0.3, "soc_percent": 39}],
                        },
                    },
                }
            ),
            encoding="utf-8",
        )
        try:
            with patch.object(mesh, "SETTINGS_FILE", path):
                mesh._last_good_path = ""
                mesh._last_good = {}
                mesh._remember_member("raspberrypi2-1", "100.91.87.103", 0.4, None, None, None)

                def wipe(data):
                    data["invite_code"] = ""
                    data["members"] = {
                        "raspberrypi2-1": {
                            "name": "raspberrypi2-1",
                            "history": [],
                            "feed_in_kwh": None,
                            "connected_kwh": 0,
                        }
                    }

                mesh._update(wipe)
            stored = json.loads(path.read_text(encoding="utf-8"))
            martin = stored["members"]["raspberrypi2-1"]
            ludwig = stored["members"]["raspberrypi2"]
            self.assertEqual(stored["invite_code"], "EG-AAAA-BBBB")
            self.assertGreaterEqual(len(martin["history"]), 5)
            self.assertEqual(martin["history"][0]["soc_percent"], 40)
            self.assertEqual(martin["feed_in_kwh"], 1.25)
            self.assertGreaterEqual(martin["connected_kwh"], 4.5)
            self.assertEqual(martin["alias"], "Haus Martin")
            self.assertEqual(ludwig["alias"], "Ludwig")
            self.assertEqual(ludwig["feed_in_kwh"], 2.5)
            self.assertEqual(len(ludwig["history"]), 1)
        finally:
            path.unlink(missing_ok=True)
            path.with_name(path.name + ".tmp").unlink(missing_ok=True)

    def test_page_save_does_not_replace_a_torn_settings_file(self):
        import energiegemeinschaft as page

        path = self._path()
        torn = '{"invite_code": "EG-AAAA-BBBB", "members": {"raspberrypi2": '
        path.write_text(torn, encoding="utf-8")
        try:
            with patch.object(page, "SETTINGS_FILE", path):
                page.save_settings("geag")
                self.assertEqual(path.read_text(encoding="utf-8"), torn)
        finally:
            path.unlink(missing_ok=True)


class TestCommunitySurplus(unittest.TestCase):
    def test_fresh_member_exports_add_up(self):
        from energiegemeinschaft_mesh import peer_surplus_kw

        rows = [
            {"alias": "Haus Martin", "feed_in_kw": 0.007, "fresh": True},
            {"alias": "Ludwig", "feed_in_kw": 0.326, "fresh": True},
            {"alias": "veraltet", "feed_in_kw": 2.0, "fresh": False},
        ]
        self.assertEqual(peer_surplus_kw(rows), 0.333)

    def test_pv_site_cannot_enable_community(self):
        from energie_steuerung import _normalize

        data = {
            "boersentarif_besitze": False,
            "boersentarif_nutzen": False,
            "gemeinschaft_bin_mitglied": True,
            "gemeinschaft_nutzen": True,
            "algorithmische_planung": True,
            "autopilot": True,
        }
        out = _normalize(dict(data), has_pv=True)
        self.assertFalse(out["gemeinschaft_bin_mitglied"])
        self.assertFalse(out["gemeinschaft_nutzen"])
        self.assertFalse(out["autopilot"])
        kept = _normalize(dict(data), has_pv=False)
        self.assertTrue(kept["gemeinschaft_nutzen"])


class TestBoersenhandelLabel(unittest.TestCase):
    def test_label_needs_both_switches(self):
        from energie_steuerung import boersenhandel_aktiv

        self.assertFalse(boersenhandel_aktiv({"boersentarif_besitze": False, "boersentarif_nutzen": False}))
        self.assertFalse(boersenhandel_aktiv({"boersentarif_besitze": True, "boersentarif_nutzen": False}))
        self.assertFalse(boersenhandel_aktiv({"boersentarif_besitze": False, "boersentarif_nutzen": True}))
        self.assertTrue(boersenhandel_aktiv({"boersentarif_besitze": True, "boersentarif_nutzen": True}))


if __name__ == "__main__":
    unittest.main()
