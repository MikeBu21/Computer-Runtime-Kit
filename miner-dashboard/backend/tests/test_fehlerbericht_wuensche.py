# -*- coding: utf-8 -*-
"""Änderungswünsche liegen in der Outbox und hängen am nächsten echten Versand."""
from __future__ import annotations

import gzip
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

import error_reports_ui


def _sender():
    path = (
        Path(__file__).resolve().parents[3]
        / "scripts"
        / "error_log_pipeline"
        / "pi"
        / "send_telegram.py"
    )
    spec = importlib.util.spec_from_file_location("send_telegram_wishes", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class WishesOutboxTests(unittest.TestCase):
    def test_save_puts_text_in_the_outbox_and_empty_removes_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = {"outbox_dir": tmp}
            saved = error_reports_ui.save_wishes("  Bitte die Kurve glätten.  ", cfg)
            path = Path(tmp) / "steuerung-wuensche.txt"
            self.assertEqual(saved, "Bitte die Kurve glätten.")
            self.assertEqual(path.read_text(encoding="utf-8").strip(), saved)
            self.assertEqual(error_reports_ui.load_wishes(cfg), saved)
            self.assertEqual(error_reports_ui.save_wishes("  ", cfg), "")
            self.assertFalse(path.exists())

    def test_report_carries_wishes_and_a_real_send_archives_them(self):
        sender = _sender()
        with tempfile.TemporaryDirectory() as tmp:
            outbox = Path(tmp) / "outbox"
            sent = Path(tmp) / "sent"
            outbox.mkdir()
            (outbox / "steuerung-wuensche.txt").write_text(
                "Schaltfläche größer\n", encoding="utf-8"
            )
            gz = outbox / "piminer-errors-demo.jsonl.gz"
            with gzip.open(gz, "wt", encoding="utf-8") as handle:
                handle.write(json.dumps({"ts": "t", "source": "log", "line": "boom"}) + "\n")
            report = sender.append_wishes(
                sender.build_plaintext_report(gz, {"event_count": 1, "sources": ["log"]}),
                sender.read_wishes(outbox),
            )
            self.assertIn("Änderungswünsche an der Steuerung", report)
            self.assertIn("Schaltfläche größer", report)
            self.assertIn("boom", report)
            sender.archive_wishes(outbox, sent)
            self.assertFalse((outbox / "steuerung-wuensche.txt").exists())
            self.assertEqual(
                (sent / "steuerung-wuensche.txt").read_text(encoding="utf-8").strip(),
                "Schaltfläche größer",
            )


if __name__ == "__main__":
    unittest.main()
