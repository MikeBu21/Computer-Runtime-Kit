# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from device_registry import execute_device_action
from manual_switch_notify import (
    device_identity,
    format_manual_switch_message,
    switch_verb,
)


class TestManualSwitchNotify(unittest.TestCase):
    def test_verbs_on_off_pause(self):
        self.assertEqual(switch_verb("resume"), "eingeschaltet")
        self.assertEqual(switch_verb("on"), "eingeschaltet")
        self.assertEqual(switch_verb("pause"), "pausiert")
        self.assertEqual(switch_verb("off"), "ausgeschaltet")

    def test_pause_message_uses_chat_id(self):
        text = format_manual_switch_message(
            {"chat_id": 0, "source": "telegram"},
            "Beispiel",
            "pause",
            ip="192.0.2.10",
        )
        self.assertEqual(
            text,
            "Nutzer mit Chat-ID 0 hat Gerät Hydro (192.0.2.10) pausiert.",
        )

    def test_on_message_dashboard(self):
        text = format_manual_switch_message(
            {"source": "dashboard", "username": "miningfarm"},
            "Hydro",
            "resume",
            ip="192.0.2.10",
        )
        self.assertEqual(
            text,
            "Nutzer mit Dashboard-Konto miningfarm hat Gerät Hydro (192.0.2.10) eingeschaltet.",
        )

    def test_identity_includes_full_ip_without_note_number(self):
        self.assertEqual(
            device_identity(
                {"type": "miner", "ip": "192.0.2.10", "label": "Beispiel"}
            ),
            "Hydro (192.0.2.10)",
        )
        self.assertEqual(
            device_identity(
                {"type": "miner", "ip": "192.0.2.10", "label": "Beispiel"}
            ),
            "Hydro (192.0.2.10)",
        )

    @patch("manual_switch_notify.notify_manual_switch")
    @patch("device_registry.execute_miner_command", return_value=True)
    def test_manual_source_notifies(self, _cmd, mock_notify):
        ok = execute_device_action(
            {"type": "miner", "ip": "192.0.2.10"},
            "pause",
            source="manual",
            actor={"chat_id": 1},
        )
        self.assertTrue(ok)
        mock_notify.assert_called_once()

    @patch("manual_switch_notify.notify_manual_switch")
    @patch("device_registry.execute_miner_command", return_value=True)
    def test_auto_source_does_not_notify(self, _cmd, mock_notify):
        ok = execute_device_action(
            {"type": "miner", "ip": "192.0.2.10"},
            "pause",
            source="auto",
        )
        self.assertTrue(ok)
        mock_notify.assert_not_called()


if __name__ == "__main__":
    unittest.main()
