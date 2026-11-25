# -*- coding: utf-8 -*-
"""Fehlerbericht-Erfassung: Erkennung, Gruppierung und Bericht aus App-Logs."""
from __future__ import annotations

import gzip
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

PIPELINE = Path(__file__).resolve().parents[3] / "scripts" / "error_log_pipeline" / "pi"
sys.path.insert(0, str(PIPELINE))

import fault_scan  # noqa: E402

LOG = """\
[2026-11-20T23:21:53+01:00] Start Telegram Bot
[Info] alles gut, error=0
Traceback (most recent call last):
  File "/home/piminer/minersteuerung/miner-dashboard/backend/app.py", line 12, in handler
    do_it()
  File "/home/piminer/minersteuerung/venv/lib/python3.13/site-packages/x.py", line 3, in do_it
    raise ValueError("kaputt 17")
ValueError: kaputt 17

The above exception was the direct cause of the following exception:

Traceback (most recent call last):
  File "/home/piminer/minersteuerung/miner-dashboard/backend/app.py", line 20, in outer
    handler()
RuntimeError: Aufruf fehlgeschlagen
[2026-11-20T23:22:00+01:00] [Warnung] Polling-Netzwerkfehler: Timed out – Neustart in 5s
[2026-11-20T23:23:00+01:00] [Warnung] Polling-Netzwerkfehler: Timed out – Neustart in 9s
[192.0.2.10] Status: error | Hashrate: —
\x1b[31m\x1b[1mWARNING: This is a development server. Do not use it in a production deployment.\x1b[0m
10.0.0.5 - - [20/Nov/2026 23:24:00] "GET /api/x?a=1 HTTP/1.1" 500 -
10.0.0.5 - - [20/Nov/2026 23:24:01] "GET /fehlerberichte HTTP/1.1" 200 -
[2026-11-20 23:25:00] [Steuerung] Daemon gestartet | Tick-Timeout=180s
"""


def _cfg(tmp: Path) -> dict:
    logs = tmp / "logs"
    logs.mkdir()
    (logs / "Telegram_Bot.log").write_text(LOG, encoding="utf-8")
    cfg = fault_scan.load_config(None)
    cfg.update(
        log_dirs=[str(logs)],
        store_file=str(tmp / "findings.json"),
        state_file=str(tmp / "state.json"),
        buffer_dir=str(tmp / "buffer"),
        outbox_dir=str(tmp / "outbox"),
        sent_dir=str(tmp / "sent"),
        journal=False,
        repo=str(tmp),
    )
    return cfg


def _scan_logs(cfg: dict) -> dict:
    store = fault_scan.load_store(cfg, now=time.time())
    store["period_start"] = 0
    fault_scan.scan_app_logs(store, cfg, time.time())
    return store


class FaultScanTests(unittest.TestCase):
    def test_app_log_findings_are_classified_and_grouped(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _scan_logs(_cfg(Path(tmp)))
            rows = {f["title"]: f for f in store["findings"].values()}

            tb = rows["RuntimeError: Aufruf fehlgeschlagen"]
            self.assertEqual(tb["severity"], "error")
            self.assertIn("app.py:20 in outer", tb["location"])
            self.assertIn("ValueError: kaputt 17", tb["message"])

            warn = rows["[Warnung] Polling-Netzwerkfehler: Timed out – Neustart in 5s"]
            self.assertEqual((warn["severity"], warn["category"], warn["count"]), ("warning", "Netzwerk", 2))

            self.assertEqual(rows["Gerät 192.0.2.10 meldet Status error"]["category"], "Gerät")
            self.assertEqual(rows["HTTP 500 bei /api/x"]["severity"], "error")

            titles = " ".join(rows)
            self.assertNotIn("Daemon gestartet", titles)
            self.assertNotIn("error=0", titles)
            self.assertNotIn("/fehlerberichte", titles)
            self.assertEqual(store["suppressed"].get("Flask-Entwicklungsserver-Hinweis"), 1)

    def test_rescan_only_reads_new_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(Path(tmp))
            store = _scan_logs(cfg)
            before = sum(f["count"] for f in store["findings"].values())
            fault_scan.scan_app_logs(store, cfg, time.time())
            self.assertEqual(sum(f["count"] for f in store["findings"].values()), before)

    def test_unit_failure_lines_become_one_finding(self):
        store = {"findings": {}, "suppressed": {}}
        for msg, counted in (
            ("x.service: Failed at step EXEC spawning /a.sh: No such file or directory", False),
            ("x.service: Failed with result 'exit-code'.", True),
            ("Failed to start x.service - X.", False),
        ):
            fault_scan.add_finding(
                store, fp="u", ts=time.time(), severity="error", category="Dienst", source="s",
                origin="journal", title="Dienst x.service ist fehlgeschlagen", message=msg,
                counted=counted, merge_message=True,
            )
        row = store["findings"]["u"]
        self.assertEqual(row["count"], 1)
        self.assertEqual(len(row["message"].splitlines()), 3)

    def test_collect_writes_readable_package_and_restarts_period(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(Path(tmp))
            fault_scan.save_store(cfg, _scan_logs(cfg))
            gz, meta_path = fault_scan.collect(cfg)
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            self.assertEqual(meta["report_version"], 2)
            self.assertGreater(meta["severity_counts"]["error"], 0)
            findings = [json.loads(ln) for ln in gzip.decompress(gz.read_bytes()).decode().splitlines()]
            text = fault_scan.render_text(meta, findings)
            self.assertIn("SYSTEMZUSTAND", text)
            self.assertIn("RuntimeError: Aufruf fehlgeschlagen", text)
            self.assertIn("Empfehlung:", text)
            self.assertEqual(fault_scan.load_store(cfg)["findings"], {})

    def test_nothing_to_report_queues_no_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(Path(tmp))
            (Path(tmp) / "logs" / "Telegram_Bot.log").write_text("[2026-11-20 10:00:00] alles ok\n", encoding="utf-8")
            cfg["min_send_severity"] = "critical"
            self.assertIsNone(fault_scan.collect(cfg))
            self.assertFalse(any((Path(tmp) / "outbox").glob("*.jsonl.gz")))


if __name__ == "__main__":
    unittest.main()
