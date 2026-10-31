# -*- coding: utf-8 -*-
import json
import unittest
from unittest.mock import patch

from energiegemeinschaft_mesh import (
    code_is_valid,
    command_packet,
    feed_packet,
    generate_invite_code,
    handle_datagram,
    verify_packet,
)


class TestEnergiegemeinschaftMesh(unittest.TestCase):
    def test_code_format(self):
        code = generate_invite_code()
        self.assertTrue(code_is_valid(code))
        self.assertTrue(code.startswith("EG-"))
        self.assertEqual(len(code), 12)
        self.assertTrue(code_is_valid("EG-7K2M-Q4HP"))

    def test_feed_packet_carries_export_and_soc_and_checks_out(self):
        code = "EG-7K2M-Q4HP"
        packet = feed_packet("haus-nord", 1.25, 71, code)
        self.assertEqual(packet["feed_in_kw"], 1.25)
        self.assertEqual(packet["soc_percent"], 71.0)
        self.assertTrue(verify_packet(packet, code))
        self.assertFalse(verify_packet(packet, "EG-AAAA-AAAA"))

    def test_command_is_addressed_to_one_pi(self):
        code = "EG-7K2M-Q4HP"
        packet = command_packet("piminer", "haus-nord", "heute Abend drosseln", code)
        self.assertEqual(packet["kind"], "cmd")
        self.assertEqual(packet["to"], "haus-nord")
        self.assertNotEqual(packet["to"], "piminer")
        self.assertTrue(verify_packet(packet, code))

    def test_listener_drops_packet_without_valid_code(self):
        packet = feed_packet("fremd", 0.4, 50, "EG-7K2M-Q4HP")
        raw = json.dumps(packet).encode("utf-8")
        with patch("energiegemeinschaft_mesh.mesh_settings", return_value={"invite_code": "EG-AAAA-BBBB"}):
            with patch("energiegemeinschaft_mesh._remember_member") as remember:
                handle_datagram(raw, "100.1.1.1")
        remember.assert_not_called()


if __name__ == "__main__":
    unittest.main()
