# -*- coding: utf-8 -*-
"""Tests Steuerungs-Protokoll."""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta

from pathlib import Path

from control_algorithm.steering_history import (
    build_steering_stats_payload,
    format_duration,
    load_steering_history,
    record_steering_actions,
)


class TestSteeringHistory(unittest.TestCase):
    def test_format_duration(self):
        self.assertEqual(format_duration(45), "45 s")
        self.assertEqual(format_duration(125), "2 min")
        self.assertEqual(format_duration(3700), "1 h 1 min")

    def test_pause_resume_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            t0 = datetime.now() - timedelta(hours=3)
            t1 = t0 + timedelta(hours=2, minutes=15)
            record_steering_actions(
                tmp,
                [{"ip": "10.0.0.1", "action": "pause", "reason": "Budget knapp", "dry_run": True}],
                {"10.0.0.1": "Miner A"},
                now=t0,
            )
            record_steering_actions(
                tmp,
                [{"ip": "10.0.0.1", "action": "resume", "reason": "Budget reicht"}],
                {"10.0.0.1": "Miner A"},
                now=t1,
            )
            payload = build_steering_stats_payload(tmp)
            self.assertEqual(payload["summary"]["completed_sessions"], 1)
            self.assertEqual(len(payload["recent_sessions"]), 1)
            row = payload["recent_sessions"][0]
            self.assertEqual(row["name"], "Miner A")
            self.assertEqual(row["pause_reason"], "Budget knapp")
            self.assertEqual(row["resume_reason"], "Budget reicht")
            self.assertEqual(row["duration_sec"], 8100.0)
            # Event-Log enthält Aus und Ein
            actions = [e["action"] for e in payload["recent_events"]]
            self.assertIn("pause", actions)
            self.assertIn("resume", actions)
            self.assertEqual(payload["summary"]["total_resume_events"], 1)

    def test_active_pause_duration(self):
        with tempfile.TemporaryDirectory() as tmp:
            t0 = datetime.now() - timedelta(minutes=30)
            record_steering_actions(
                tmp,
                [{"ip": "10.0.0.2", "action": "pause", "reason": "Anomalie", "force": True}],
                {"10.0.0.2": "Miner B"},
                now=t0,
            )
            payload = build_steering_stats_payload(tmp)
            self.assertEqual(payload["summary"]["currently_paused"], 1)
            self.assertEqual(len(payload["active_pauses"]), 1)
            self.assertGreater(payload["active_pauses"][0]["duration_sec"], 1700)
            self.assertEqual(payload["active_pauses"][0]["resumed_at_display"], "noch aus")

    def test_no_duplicate_pause_while_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            t0 = datetime.now() - timedelta(hours=1)
            t1 = t0 + timedelta(minutes=5)
            record_steering_actions(
                tmp,
                [{"ip": "10.0.0.3", "action": "pause", "reason": "Nacht", "force": True}],
                {"10.0.0.3": "Miner C"},
                now=t0,
            )
            record_steering_actions(
                tmp,
                [{"ip": "10.0.0.3", "action": "pause", "reason": "Nacht erneut", "force": True}],
                {"10.0.0.3": "Miner C"},
                now=t1,
            )
            store = load_steering_history(tmp)
            self.assertEqual(len(store["sessions"]), 1)
            # Grund wird aktualisiert, aber nur EIN Aus-Event (kein Spam)
            self.assertEqual(store["sessions"][0]["pause_reason"], "Nacht erneut")
            pause_events = [e for e in store["events"] if e.get("action") == "pause"]
            self.assertEqual(len(pause_events), 1)
            self.assertEqual(pause_events[0]["reason"], "Nacht")

    def test_silent_close_open_pause_without_event(self):
        from control_algorithm.steering_history import close_open_pauses_silently

        with tempfile.TemporaryDirectory() as tmp:
            t0 = datetime.now() - timedelta(minutes=20)
            record_steering_actions(
                tmp,
                [{"ip": "10.0.0.77", "action": "pause", "reason": "Budget"}],
                {"10.0.0.77": "Miner S"},
                now=t0,
            )
            close_open_pauses_silently(tmp, {"10.0.0.77"}, now=datetime.now())
            store = load_steering_history(tmp)
            self.assertEqual(len(store["events"]), 1)
            self.assertEqual(store["events"][0]["action"], "pause")
            self.assertEqual(store["open_by_ip"], {})
            self.assertTrue(store["sessions"][0].get("silent_sync"))
            payload = build_steering_stats_payload(tmp)
            self.assertEqual(payload["summary"]["total_resume_events"], 0)
            self.assertEqual(len(payload["recent_sessions"]), 0)

    def test_backfill_events_from_legacy_sessions(self):
        """Alte Dateien ohne events[] müssen im UI trotzdem Schaltungen zeigen."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "control_algorithm_steering_history.json"
            t0 = (datetime.now() - timedelta(hours=2)).isoformat()
            path.write_text(
                json.dumps(
                    {
                        "sessions": [
                            {
                                "ip": "10.0.0.11",
                                "name": "Legacy",
                                "paused_at": t0,
                                "pause_reason": "alt ohne Event",
                                "resumed_at": None,
                                "resume_reason": None,
                                "duration_sec": None,
                                "dry_run": False,
                                "force": False,
                            }
                        ],
                        "open_by_ip": {"10.0.0.11": 0},
                        "events": [],
                    }
                ),
                encoding="utf-8",
            )
            payload = build_steering_stats_payload(tmp)
            self.assertEqual(len(payload["recent_events"]), 1)
            self.assertEqual(payload["recent_events"][0]["action"], "pause")
            self.assertEqual(payload["recent_events"][0]["reason"], "alt ohne Event")
            store = load_steering_history(tmp)
            self.assertEqual(len(store["events"]), 1)

    def test_save_verify_persists_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            record_steering_actions(
                tmp,
                [{"ip": "10.0.0.12", "action": "pause", "reason": "persist"}],
                {"10.0.0.12": "P"},
            )
            record_steering_actions(
                tmp,
                [{"ip": "10.0.0.12", "action": "resume", "reason": "persist ein"}],
                {"10.0.0.12": "P"},
            )
            disk = json.loads(
                (Path(tmp) / "control_algorithm_steering_history.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(len(disk["events"]), 2)
            self.assertEqual(
                [e["action"] for e in disk["events"]], ["pause", "resume"]
            )

    def test_resume_without_open_pause_is_documented(self):
        """Einschaltung ohne offene Pause muss trotzdem dokumentiert werden."""
        with tempfile.TemporaryDirectory() as tmp:
            t0 = datetime.now() - timedelta(minutes=10)
            record_steering_actions(
                tmp,
                [{"ip": "10.0.0.9", "action": "resume", "reason": "Energie-Budget reicht"}],
                {"10.0.0.9": "Miner Solo"},
                now=t0,
            )
            store = load_steering_history(tmp)
            self.assertEqual(len(store["sessions"]), 1)
            self.assertTrue(store["sessions"][0].get("resume_only"))
            self.assertEqual(store["sessions"][0]["resume_reason"], "Energie-Budget reicht")
            self.assertEqual(len(store["events"]), 1)
            self.assertEqual(store["events"][0]["action"], "resume")

            payload = build_steering_stats_payload(tmp)
            self.assertEqual(payload["summary"]["total_resume_events"], 1)
            self.assertEqual(len(payload["recent_events"]), 1)
            self.assertEqual(payload["recent_events"][0]["action_label"], "Einschalten")
            self.assertEqual(len(payload["recent_sessions"]), 1)
            self.assertEqual(payload["recent_sessions"][0]["resumed_at_display"][:2], t0.strftime("%d"))

    def test_prune_keeps_open_pause_older_than_retention(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_open = datetime.now() - timedelta(days=45)
            record_steering_actions(
                tmp,
                [{"ip": "10.0.0.6", "action": "pause", "reason": "alt offen"}],
                {"10.0.0.6": "Offen"},
                now=old_open,
            )
            # Resume danach muss die alte Pause schließen und dokumentieren
            record_steering_actions(
                tmp,
                [{"ip": "10.0.0.6", "action": "resume", "reason": "jetzt ein"}],
                {"10.0.0.6": "Offen"},
                now=datetime.now(),
            )
            store = load_steering_history(tmp)
            self.assertEqual(len(store["sessions"]), 1)
            self.assertEqual(store["sessions"][0]["resume_reason"], "jetzt ein")
            self.assertIsNotNone(store["sessions"][0]["resumed_at"])

    def test_stats_payload_does_not_rewrite_unchanged_store(self):
        """Status-API darf unveränderte History nicht neu schreiben (Race mit Tick)."""
        with tempfile.TemporaryDirectory() as tmp:
            t0 = datetime.now() - timedelta(minutes=10)
            record_steering_actions(
                tmp,
                [{"ip": "10.0.0.8", "action": "pause", "reason": "Budget"}],
                {"10.0.0.8": "Miner X"},
                now=t0,
            )
            path = Path(tmp) / "control_algorithm_steering_history.json"
            mtime_before = path.stat().st_mtime
            payload = build_steering_stats_payload(tmp)
            self.assertEqual(payload["summary"]["currently_paused"], 1)
            mtime_after = path.stat().st_mtime
            self.assertEqual(mtime_before, mtime_after)

    def test_record_persists_under_concurrent_stats_reads(self):
        """Tick-Events müssen trotz paralleler Stats-Reads auf Disk landen."""
        with tempfile.TemporaryDirectory() as tmp:
            t0 = datetime.now() - timedelta(minutes=5)
            # Offene Pause anlegen
            record_steering_actions(
                tmp,
                [{"ip": "10.0.0.7", "action": "pause", "reason": "alt aus"}],
                {"10.0.0.7": "Miner Y"},
                now=t0,
            )
            # Stats lesen (früher: hat immer gespeichert und konnte Tick überschreiben)
            build_steering_stats_payload(tmp)
            record_steering_actions(
                tmp,
                [{"ip": "10.0.0.7", "action": "resume", "reason": "jetzt ein"}],
                {"10.0.0.7": "Miner Y"},
                now=t0 + timedelta(minutes=2),
            )
            build_steering_stats_payload(tmp)
            store = load_steering_history(tmp)
            actions = [e["action"] for e in store.get("events") or []]
            self.assertEqual(actions.count("pause"), 1)
            self.assertEqual(actions.count("resume"), 1)
            self.assertNotIn("10.0.0.7", store.get("open_by_ip") or {})

    def test_prune_older_than_retention_days(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = datetime.now() - timedelta(days=45)
            recent = datetime.now() - timedelta(hours=5)
            record_steering_actions(
                tmp,
                [{"ip": "10.0.0.4", "action": "pause", "reason": "alt"}],
                {"10.0.0.4": "Alt"},
                now=old,
            )
            record_steering_actions(
                tmp,
                [{"ip": "10.0.0.4", "action": "resume", "reason": "alt ein"}],
                now=old + timedelta(hours=1),
            )
            record_steering_actions(
                tmp,
                [{"ip": "10.0.0.5", "action": "pause", "reason": "neu"}],
                {"10.0.0.5": "Neu"},
                now=recent,
            )
            payload = build_steering_stats_payload(tmp)
            store = load_steering_history(tmp)
            self.assertEqual(len(store["sessions"]), 1)
            self.assertEqual(store["sessions"][0]["ip"], "10.0.0.5")
            self.assertEqual(payload["summary"]["currently_paused"], 1)


if __name__ == "__main__":
    unittest.main()
