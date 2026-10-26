# -*- coding: utf-8 -*-
"""Tests Anti-Flatter Commitments + Soft-Tageslimit."""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from control_algorithm.switch_commitment import (
    apply_switch_commitment_guards,
    is_emergency_action,
    off_count_today,
    sync_plan_times_into_runtime,
    update_runtime_commitments,
)


class TestEmergencyDetection(unittest.TestCase):
    def test_min_soc_reason(self):
        self.assertTrue(
            is_emergency_action(
                {"reason": "Mindest-SoC (45%) unterschritten (aktuell 40%)", "force": True}
            )
        )

    def test_morning_floor_not_emergency(self):
        self.assertFalse(
            is_emergency_action(
                {
                    "reason": "Morgen-Floor (≥46 %): Last 22.0 kW – 5 aus",
                    "morning_floor": True,
                }
            )
        )


class TestSameTickArbiter(unittest.TestCase):
    def test_pause_wins_over_export_resume(self):
        actions = [
            {
                "ip": "10.0.0.1",
                "action": "resume",
                "reason": "Netzeinspeisung 0.5 kW vermeiden",
                "urgent": True,
            },
            {
                "ip": "10.0.0.1",
                "action": "pause",
                "reason": "Morgen-Floor (≥46 %): aus",
                "morning_floor": True,
            },
        ]
        out, msgs = apply_switch_commitment_guards(
            actions, {"device_runtime": {}}, {"max_device_off_per_day": 3}, live={}
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["action"], "pause")

    def test_floor_pause_wins_over_fill_urgency_same_ip(self):
        """Anti-Flatter Midday: Floor/Budget-Pause schlägt Fill auf derselben IP."""
        actions = [
            {
                "ip": "10.0.0.1",
                "action": "pause",
                "reason": "Morgen-Floor (≥46 %): aus",
                "morning_floor": True,
                "budget_shed": True,
                "load_overhang_kw": 14.0,
            },
            {
                "ip": "10.0.0.1",
                "action": "resume",
                "reason": "Flache Füllkurve: Flex gegen Max-SoC",
                "source": "fill_schedule",
                "fill_urgency": True,
                "urgent": True,
            },
        ]
        out, _ = apply_switch_commitment_guards(
            actions,
            {"device_runtime": {}},
            {"max_device_off_per_day": 3, "budget_commitment_override_kw": 3.0},
            live={},
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["action"], "pause")
        self.assertTrue(out[0].get("morning_floor"))

    def test_spaetstart_fill_urgency_wins_over_floor_when_urgent(self):
        """EMS: Fill mit hours_to_max/overflow schlägt Floor-Pause auf derselben IP."""
        state = {
            "device_runtime": {},
            "live_soc": 88.0,
            "fill_schedule": {
                "ok": True,
                "flex": {
                    "should_start_now": True,
                    "overflow_kwh": 20.0,
                    "ip": "10.0.0.1",
                },
                "flex_stack": [{"ip": "10.0.0.1"}],
                "excess_charge_kw": 30.0,
                "hours_to_max": 0.5,
                "soc_percent": 88.0,
            },
        }
        actions = [
            {
                "ip": "10.0.0.1",
                "action": "pause",
                "reason": "Morgen-Floor (≥46 %): aus",
                "morning_floor": True,
                "budget_shed": True,
                "load_overhang_kw": 10.0,
            },
            {
                "ip": "10.0.0.1",
                "action": "resume",
                "reason": "Spätstart-Entladen, Priorität 1",
                "urgent": True,
            },
        ]
        out, _ = apply_switch_commitment_guards(
            actions,
            state,
            {
                "max_device_off_per_day": 3,
                "fill_commitment_override_hours_to_max": 2.0,
                "fill_commitment_override_excess_kw": 5.0,
                "budget_commitment_override_kw": 3.0,
                "max_soc_percent": 95.0,
            },
            live={},
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["action"], "resume")
        self.assertTrue(out[0].get("fill_urgency"))


class TestOffCommitmentBlocksResume(unittest.TestCase):
    def test_spaetstart_blocked_while_committed_off(self):
        now = datetime.now()
        until = (now + timedelta(hours=2)).isoformat()
        state = {
            "device_runtime": {
                "10.0.0.2": {
                    "committed_off_until": until,
                    "paused_at": now.isoformat(),
                    "off_count_today": 1,
                    "off_count_date": now.date().isoformat(),
                }
            }
        }
        actions = [
            {
                "ip": "10.0.0.2",
                "action": "resume",
                "reason": "Spätstart-Fenster: Speicher 72 % → 46 %",
                "urgent": True,
            }
        ]
        out, msgs = apply_switch_commitment_guards(
            actions,
            state,
            {"max_device_off_per_day": 3, "export_commitment_override_kw": 2.0},
            live={"einspeisung_kw": 0.2},
            now=now,
        )
        self.assertEqual(out, [])
        self.assertTrue(any("Commitment aus" in m for m in msgs))

    def test_high_export_overrides_off_commitment(self):
        now = datetime.now()
        until = (now + timedelta(hours=2)).isoformat()
        state = {
            "device_runtime": {
                "10.0.0.3": {"committed_off_until": until, "paused_at": now.isoformat()}
            }
        }
        actions = [
            {
                "ip": "10.0.0.3",
                "action": "resume",
                "reason": "Netzeinspeisung 3.50 kW vermeiden – Gerät ein",
                "urgent": True,
            }
        ]
        out, msgs = apply_switch_commitment_guards(
            actions,
            state,
            {"max_device_off_per_day": 3, "export_commitment_override_kw": 2.0},
            live={"einspeisung_kw": 3.5},
            now=now,
        )
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0].get("commitment_export_override"))

    def test_fill_resume_without_urgency_blocked(self):
        now = datetime.now()
        until = (now + timedelta(hours=2)).isoformat()
        state = {
            "device_runtime": {
                "10.0.0.10": {
                    "committed_off_until": until,
                    "paused_at": now.isoformat(),
                }
            },
            "fill_schedule": {
                "ok": True,
                "flex": {"should_start_now": False, "overflow_kwh": 0},
                "excess_charge_kw": 1.0,
                "hours_to_max": 5.0,
            },
        }
        actions = [
            {
                "ip": "10.0.0.10",
                "action": "resume",
                "reason": "Flache Füllkurve: Anker zuerst",
                "source": "fill_schedule",
                "fill_urgency": False,
                "urgent": True,
            }
        ]
        out, msgs = apply_switch_commitment_guards(
            actions,
            state,
            {
                "max_device_off_per_day": 3,
                "fill_commitment_override_hours_to_max": 2.0,
                "fill_commitment_override_excess_kw": 5.0,
            },
            live={},
            now=now,
        )
        self.assertEqual(out, [])
        self.assertTrue(any("Commitment aus" in m for m in msgs))

    def test_fill_urgency_overrides_off_commitment(self):
        now = datetime.now()
        until = (now + timedelta(hours=2)).isoformat()
        state = {
            "device_runtime": {
                "10.0.0.11": {
                    "committed_off_until": until,
                    "paused_at": now.isoformat(),
                }
            }
        }
        actions = [
            {
                "ip": "10.0.0.11",
                "action": "resume",
                "reason": "Flache Füllkurve: Flex gegen Max-SoC",
                "source": "fill_schedule",
                "fill_urgency": True,
                "urgent": True,
            }
        ]
        out, msgs = apply_switch_commitment_guards(
            actions,
            state,
            {
                "max_device_off_per_day": 3,
                "fill_commitment_override_hours_to_max": 2.0,
                "fill_commitment_override_excess_kw": 5.0,
            },
            live={},
            now=now,
        )
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0].get("commitment_fill_override"))
        self.assertTrue(any("Max-SoC-Füll" in m for m in msgs))

    def test_fill_urgency_via_plan_hours_to_max(self):
        now = datetime.now()
        until = (now + timedelta(hours=2)).isoformat()
        state = {
            "device_runtime": {
                "10.0.0.12": {
                    "committed_off_until": until,
                    "paused_at": now.isoformat(),
                }
            },
            "fill_schedule": {
                "ok": True,
                "flex": {
                    "should_start_now": True,
                    "overflow_kwh": 0.1,
                    "ip": "10.0.0.12",
                },
                "flex_stack": [{"ip": "10.0.0.12", "should_start_now": True}],
                "excess_charge_kw": 1.0,
                "hours_to_max": 1.3,
            },
        }
        actions = [
            {
                "ip": "10.0.0.12",
                "action": "resume",
                "reason": "Füll-Plan: Flex",
                "source": "fill_schedule",
                "fill_urgency": False,
                "urgent": True,
            }
        ]
        out, msgs = apply_switch_commitment_guards(
            actions,
            state,
            {
                "max_device_off_per_day": 3,
                "fill_commitment_override_hours_to_max": 2.0,
                "fill_commitment_override_excess_kw": 5.0,
            },
            live={},
            now=now,
        )
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0].get("commitment_fill_override"))

    def test_spaetstart_resume_overrides_when_fill_urgent(self):
        """Regression: Resume kam als Spätstart, Füllplan wollte aber sofort."""
        now = datetime.now()
        until = (now + timedelta(hours=2)).isoformat()
        state = {
            "device_runtime": {
                "192.168.1.149": {
                    "committed_off_until": until,
                    "paused_at": now.isoformat(),
                }
            },
            "fill_schedule": {
                "ok": True,
                "flex": {
                    "should_start_now": True,
                    "overflow_kwh": 500.0,
                    "ip": "192.168.1.149",
                },
                "flex_stack": [
                    {"ip": "192.168.1.149", "should_start_now": True},
                    {"ip": "192.168.1.153", "should_start_now": True},
                ],
                "excess_charge_kw": 70.0,
                "hours_to_max": 0.8,
            },
        }
        actions = [
            {
                "ip": "192.168.1.149",
                "action": "resume",
                "reason": "Spätstart-Entladen, Priorität 1",
                "urgent": True,
            }
        ]
        out, msgs = apply_switch_commitment_guards(
            actions,
            state,
            {
                "max_device_off_per_day": 3,
                "fill_commitment_override_hours_to_max": 2.0,
                "fill_commitment_override_excess_kw": 5.0,
            },
            live={},
            now=now,
        )
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0].get("commitment_fill_override"))
        self.assertTrue(out[0].get("fill_urgency"))
        self.assertTrue(any("Max-SoC-Füll" in m for m in msgs))

    def test_spaetstart_blocked_when_fill_not_urgent(self):
        now = datetime.now()
        until = (now + timedelta(hours=2)).isoformat()
        state = {
            "device_runtime": {
                "192.168.1.149": {
                    "committed_off_until": until,
                    "paused_at": now.isoformat(),
                }
            },
            "fill_schedule": {
                "ok": True,
                "flex": {"should_start_now": False, "overflow_kwh": 0},
                "excess_charge_kw": 0.5,
                "hours_to_max": 6.0,
            },
        }
        actions = [
            {
                "ip": "192.168.1.149",
                "action": "resume",
                "reason": "Spätstart-Entladen, Priorität 1",
                "urgent": True,
            }
        ]
        out, msgs = apply_switch_commitment_guards(
            actions,
            state,
            {
                "max_device_off_per_day": 3,
                "fill_commitment_override_hours_to_max": 2.0,
                "fill_commitment_override_excess_kw": 5.0,
            },
            live={},
            now=now,
        )
        self.assertEqual(out, [])
        self.assertTrue(any("Commitment aus" in m for m in msgs))

    def test_fill_urgent_does_not_override_unwanted_ip(self):
        now = datetime.now()
        until = (now + timedelta(hours=2)).isoformat()
        state = {
            "device_runtime": {
                "10.0.0.99": {
                    "committed_off_until": until,
                    "paused_at": now.isoformat(),
                }
            },
            "fill_schedule": {
                "ok": True,
                "flex": {
                    "should_start_now": True,
                    "overflow_kwh": 10.0,
                    "ip": "192.168.1.149",
                },
                "flex_stack": [{"ip": "192.168.1.149"}],
                "excess_charge_kw": 20.0,
                "hours_to_max": 0.5,
            },
        }
        actions = [
            {
                "ip": "10.0.0.99",
                "action": "resume",
                "reason": "Spätstart-Entladen",
                "urgent": True,
            }
        ]
        out, msgs = apply_switch_commitment_guards(
            actions,
            state,
            {
                "max_device_off_per_day": 3,
                "fill_commitment_override_hours_to_max": 2.0,
                "fill_commitment_override_excess_kw": 5.0,
            },
            live={},
            now=now,
        )
        self.assertEqual(out, [])
        self.assertTrue(any("Commitment aus" in m for m in msgs))


class TestFillOverrideThenOnCommitment(unittest.TestCase):
    def test_floor_pause_blocked_after_fill_override_resume(self):
        now = datetime.now()
        until_off = (now + timedelta(hours=2)).isoformat()
        state = {
            "device_runtime": {
                "10.0.0.13": {
                    "committed_off_until": until_off,
                    "paused_at": (now - timedelta(minutes=30)).isoformat(),
                    "off_count_today": 1,
                    "off_count_date": now.date().isoformat(),
                }
            }
        }
        cfg = {
            "max_device_off_per_day": 3,
            "switch_commitment_off_hours": 2.0,
            "min_device_runtime_hours": 3.0,
            "fill_commitment_override_hours_to_max": 2.0,
        }
        resume = {
            "ip": "10.0.0.13",
            "action": "resume",
            "reason": "Flache Füllkurve: Flex gegen Max-SoC",
            "source": "fill_schedule",
            "fill_urgency": True,
            "urgent": True,
        }
        out, _ = apply_switch_commitment_guards(
            [resume], state, cfg, live={}, now=now
        )
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0].get("commitment_fill_override"))

        state = update_runtime_commitments(state, out, cfg, now=now)
        entry = state["device_runtime"]["10.0.0.13"]
        self.assertIsNotNone(entry.get("committed_on_until"))
        self.assertIsNone(entry.get("committed_off_until"))

        pause = {
            "ip": "10.0.0.13",
            "action": "pause",
            "reason": "Morgen-Floor (≥46 %): aus",
            "morning_floor": True,
            "budget_shed": True,
            "load_overhang_kw": 1.0,
            "force": True,
        }
        out2, msgs2 = apply_switch_commitment_guards(
            [pause],
            {
                **state,
                "fill_schedule": {"ok": True, "flex": {"should_start_now": False}},
                "tick_miner_load_kw": 17.0,
                "tick_miner_budget_kw": 16.0,
            },
            {**cfg, "budget_commitment_override_kw": 3.0},
            live={},
            now=now,
        )
        self.assertEqual(out2, [])
        self.assertTrue(any("Commitment an" in m for m in msgs2))


class TestFillClearsCommittedOff(unittest.TestCase):
    def test_fill_override_clears_committed_off_in_state(self):
        now = datetime.now()
        until = (now + timedelta(hours=2)).isoformat()
        state = {
            "device_runtime": {
                "192.168.1.149": {
                    "committed_off_until": until,
                    "paused_at": now.isoformat(),
                }
            },
            "fill_schedule": {
                "ok": True,
                "flex": {
                    "should_start_now": True,
                    "overflow_kwh": 100.0,
                    "ip": "192.168.1.149",
                },
                "flex_stack": [{"ip": "192.168.1.149"}],
                "excess_charge_kw": 40.0,
                "hours_to_max": 0.5,
            },
        }
        actions = [
            {
                "ip": "192.168.1.149",
                "action": "resume",
                "reason": "Spätstart-Entladen",
                "urgent": True,
            }
        ]
        out, _ = apply_switch_commitment_guards(
            actions,
            state,
            {
                "max_device_off_per_day": 3,
                "fill_commitment_override_hours_to_max": 2.0,
                "fill_commitment_override_excess_kw": 5.0,
            },
            live={},
            now=now,
        )
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0].get("commitment_fill_override"))
        entry = state["device_runtime"]["192.168.1.149"]
        self.assertIsNone(entry.get("committed_off_until"))
        self.assertIsNotNone(entry.get("committed_off_cleared_for_fill_at"))


class TestBudgetOnCommitmentOverride(unittest.TestCase):
    def test_budget_pause_overrides_committed_on_with_overhang(self):
        now = datetime.now()
        state = {
            "device_runtime": {
                "10.0.0.20": {
                    "committed_on_until": (now + timedelta(hours=2)).isoformat(),
                    "resumed_at": now.isoformat(),
                    "off_count_today": 0,
                    "off_count_date": now.date().isoformat(),
                }
            },
            "fill_schedule": {"ok": True, "flex": {"should_start_now": False}},
            "tick_miner_load_kw": 22.0,
            "tick_miner_budget_kw": 16.0,
        }
        actions = [
            {
                "ip": "10.0.0.20",
                "action": "pause",
                "reason": "Morgen-Floor (≥46 %): Last 22.0 kW > nachhaltig 16.0 kW",
                "morning_floor": True,
                "budget_shed": True,
                "load_overhang_kw": 6.0,
                "force": True,
            }
        ]
        out, msgs = apply_switch_commitment_guards(
            actions,
            state,
            {
                "max_device_off_per_day": 3,
                "budget_commitment_override_kw": 3.0,
            },
            live={},
            now=now,
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["action"], "pause")
        self.assertTrue(out[0].get("commitment_budget_override"))
        self.assertTrue(any("Budget-Überhang" in m for m in msgs))

    def test_budget_pause_blocked_without_overhang(self):
        now = datetime.now()
        state = {
            "device_runtime": {
                "10.0.0.21": {
                    "committed_on_until": (now + timedelta(hours=2)).isoformat(),
                    "resumed_at": now.isoformat(),
                    "off_count_today": 0,
                    "off_count_date": now.date().isoformat(),
                }
            },
            "fill_schedule": {"ok": True, "flex": {"should_start_now": False}},
            "tick_miner_load_kw": 17.0,
            "tick_miner_budget_kw": 16.0,
        }
        actions = [
            {
                "ip": "10.0.0.21",
                "action": "pause",
                "reason": "Energie-Budget (16.0 kW): Last 17.0 kW · gestaffelt 1/Tick",
                "budget_shed": True,
                "load_overhang_kw": 1.0,
            }
        ]
        out, msgs = apply_switch_commitment_guards(
            actions,
            state,
            {
                "max_device_off_per_day": 3,
                "budget_commitment_override_kw": 3.0,
            },
            live={},
            now=now,
        )
        self.assertEqual(out, [])
        self.assertTrue(any("Commitment an" in m for m in msgs))

    def test_budget_override_blocked_while_fill_urgent(self):
        now = datetime.now()
        state = {
            "device_runtime": {
                "10.0.0.22": {
                    "committed_on_until": (now + timedelta(hours=2)).isoformat(),
                    "resumed_at": now.isoformat(),
                    "off_count_today": 0,
                    "off_count_date": now.date().isoformat(),
                }
            },
            "fill_schedule": {
                "ok": True,
                "flex": {
                    "should_start_now": True,
                    "overflow_kwh": 50.0,
                    "ip": "10.0.0.22",
                },
                "flex_stack": [{"ip": "10.0.0.22"}],
                "excess_charge_kw": 40.0,
                "hours_to_max": 0.4,
            },
            "tick_miner_load_kw": 28.0,
            "tick_miner_budget_kw": 16.0,
        }
        actions = [
            {
                "ip": "10.0.0.22",
                "action": "pause",
                "reason": "Morgen-Floor (≥46 %): aus",
                "morning_floor": True,
                "budget_shed": True,
                "load_overhang_kw": 12.0,
            }
        ]
        out, msgs = apply_switch_commitment_guards(
            actions,
            state,
            {
                "max_device_off_per_day": 3,
                "budget_commitment_override_kw": 3.0,
                "fill_commitment_override_hours_to_max": 2.0,
                "fill_commitment_override_excess_kw": 5.0,
            },
            live={},
            now=now,
        )
        self.assertEqual(out, [])
        self.assertTrue(any("Commitment an" in m for m in msgs))

    def test_soft_recommend_allows_budget_override_pause(self):
        now = datetime.now()
        state = {
            "device_runtime": {
                "10.0.0.23": {
                    "committed_on_until": (now + timedelta(hours=2)).isoformat(),
                    "resumed_at": now.isoformat(),
                    "off_count_today": 3,
                    "off_count_date": now.date().isoformat(),
                }
            },
            "fill_schedule": {"ok": True, "flex": {"should_start_now": False}},
        }
        actions = [
            {
                "ip": "10.0.0.23",
                "action": "pause",
                "reason": "Morgen-Floor (≥46 %): aus",
                "morning_floor": True,
                "budget_shed": True,
                "load_overhang_kw": 10.0,
            }
        ]
        out, msgs = apply_switch_commitment_guards(
            actions,
            state,
            {
                "max_device_off_per_day": 3,
                "budget_commitment_override_kw": 3.0,
            },
            live={},
            now=now,
        )
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0].get("soft_off_recommend_exceeded"))
        self.assertTrue(any("Hinweis" in m and "Empfehlung" in m for m in msgs))


class TestSoftDailyCap(unittest.TestCase):
    def test_fourth_pause_allowed_with_recommend_hint(self):
        now = datetime.now()
        state = {
            "device_runtime": {
                "10.0.0.4": {
                    "off_count_today": 3,
                    "off_count_date": now.date().isoformat(),
                    "resumed_at": (now - timedelta(hours=5)).isoformat(),
                }
            }
        }
        actions = [
            {
                "ip": "10.0.0.4",
                "action": "pause",
                "reason": "Morgen-Floor (≥46 %): aus",
                "morning_floor": True,
                "force": True,
            }
        ]
        out, msgs = apply_switch_commitment_guards(
            actions, state, {"max_device_off_per_day": 3}, live={}, now=now
        )
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0].get("soft_off_recommend_exceeded"))
        self.assertTrue(any("Hinweis" in m for m in msgs))
        self.assertFalse(any("blockiert – Soft-Limit" in m for m in msgs))

    def test_emergency_pause_bypasses_cap(self):
        now = datetime.now()
        state = {
            "device_runtime": {
                "10.0.0.5": {
                    "off_count_today": 3,
                    "off_count_date": now.date().isoformat(),
                }
            }
        }
        actions = [
            {
                "ip": "10.0.0.5",
                "action": "pause",
                "reason": "Mindest-SoC (45%) unterschritten (aktuell 40%)",
                "force": True,
                "emergency": True,
                "source": "min_soc",
            }
        ]
        out, _ = apply_switch_commitment_guards(
            actions, state, {"max_device_off_per_day": 3}, live={}, now=now
        )
        self.assertEqual(len(out), 1)


class TestPauseCandidatePrefersUnderSoftRecommend(unittest.TestCase):
    def test_prefers_device_under_recommend_count(self):
        from control_algorithm.load_scheduler import pick_pause_candidate

        devices = [
            {"ip": "10.0.0.low", "power_kw": 3.2, "priority": 0.5, "enabled": True},
            {"ip": "10.0.0.mid", "power_kw": 3.2, "priority": 1.5, "enabled": True},
        ]
        status = {"10.0.0.low": "running", "10.0.0.mid": "running"}
        now = datetime.now()
        state = {
            "device_runtime": {
                "10.0.0.low": {
                    "off_count_today": 3,
                    "off_count_date": now.date().isoformat(),
                    "resumed_at": (now - timedelta(hours=5)).isoformat(),
                },
                "10.0.0.mid": {
                    "off_count_today": 1,
                    "off_count_date": now.date().isoformat(),
                    "resumed_at": (now - timedelta(hours=5)).isoformat(),
                },
            }
        }
        cfg = {"max_device_off_per_day": 3, "min_device_runtime_hours": 0.5}
        cand = pick_pause_candidate(
            devices, status, state, cfg, respect_plan_hold=False
        )
        self.assertIsNotNone(cand)
        self.assertEqual(cand["ip"], "10.0.0.mid")


class TestOnCommitmentBlocksPause(unittest.TestCase):
    def test_morning_floor_blocked_while_committed_on(self):
        now = datetime.now()
        state = {
            "device_runtime": {
                "10.0.0.6": {
                    "committed_on_until": (now + timedelta(hours=2)).isoformat(),
                    "resumed_at": now.isoformat(),
                    "off_count_today": 0,
                    "off_count_date": now.date().isoformat(),
                }
            }
        }
        actions = [
            {
                "ip": "10.0.0.6",
                "action": "pause",
                "reason": "Morgen-Floor (≥46 %): aus",
                "morning_floor": True,
                "force": True,
            }
        ]
        out, msgs = apply_switch_commitment_guards(
            actions, state, {"max_device_off_per_day": 3}, live={}, now=now
        )
        self.assertEqual(out, [])
        self.assertTrue(any("Commitment an" in m for m in msgs))


class TestUpdateCommitments(unittest.TestCase):
    def test_pause_sets_off_commitment_and_count(self):
        now = datetime(2026, 8, 9, 12, 0, 0)
        state = {"device_runtime": {}}
        cfg = {
            "max_device_off_per_day": 3,
            "switch_commitment_off_hours": 2.0,
            "min_device_runtime_hours": 3.0,
        }
        state = update_runtime_commitments(
            state,
            [{"ip": "10.0.0.7", "action": "pause", "reason": "Morgen-Floor"}],
            cfg,
            now=now,
        )
        entry = state["device_runtime"]["10.0.0.7"]
        self.assertEqual(entry["off_count_today"], 1)
        self.assertIsNotNone(entry.get("committed_off_until"))
        self.assertIsNone(entry.get("committed_on_until"))
        until = datetime.fromisoformat(entry["committed_off_until"])
        self.assertAlmostEqual((until - now).total_seconds() / 3600.0, 2.0, places=2)

    def test_resume_uses_planned_off_within_min_runtime(self):
        now = datetime(2026, 8, 9, 12, 0, 0)
        planned_off = (now + timedelta(hours=2)).isoformat()
        state = {
            "device_runtime": {
                "10.0.0.8": {"next_off_at": planned_off}
            }
        }
        cfg = {"min_device_runtime_hours": 3.0, "switch_commitment_off_hours": 2.0}
        state = update_runtime_commitments(
            state,
            [{"ip": "10.0.0.8", "action": "resume", "reason": "Spätstart"}],
            cfg,
            now=now,
        )
        entry = state["device_runtime"]["10.0.0.8"]
        self.assertEqual(entry["committed_on_until"], planned_off)

    def test_resume_does_not_lock_on_until_next_day_noon(self):
        """Bug 01.10. 18:39: next_off_at morgen 12:12 wurde 17-h-An-Zwang."""
        now = datetime(2026, 10, 1, 18, 39, 0)
        planned_off = datetime(2026, 10, 2, 12, 12, 0).isoformat()
        state = {
            "device_runtime": {
                "192.0.2.10": {"next_off_at": planned_off}
            }
        }
        cfg = {"min_device_runtime_hours": 1.0, "switch_commitment_off_hours": 1.0}
        state = update_runtime_commitments(
            state,
            [{"ip": "192.0.2.10", "action": "resume", "reason": "Füllkurve"}],
            cfg,
            now=now,
        )
        until = datetime.fromisoformat(
            state["device_runtime"]["192.0.2.10"]["committed_on_until"]
        )
        self.assertAlmostEqual((until - now).total_seconds() / 3600.0, 1.0, places=2)


class TestSyncPlanTimes(unittest.TestCase):
    def test_sync(self):
        state = {"device_runtime": {}}
        state = sync_plan_times_into_runtime(
            state,
            [
                {
                    "ip": "10.0.0.9",
                    "planned_on": "2026-08-09T18:00:00",
                    "planned_off": "2026-08-09T22:00:00",
                }
            ],
        )
        entry = state["device_runtime"]["10.0.0.9"]
        self.assertEqual(entry["next_on_at"], "2026-08-09T18:00:00")
        self.assertEqual(entry["next_off_at"], "2026-08-09T22:00:00")


class TestMorningFloorAntiFlatter(unittest.TestCase):
    def test_fill_allowed_while_morning_floor_committed_off_when_urgent(self):
        """EMS: Fill mit Overflow/hours_to_max bricht Energiespar-Off."""
        now = datetime.now()
        until = (now + timedelta(hours=2)).isoformat()
        state = {
            "device_runtime": {
                "192.168.1.153": {
                    "committed_off_until": until,
                    "paused_at": now.isoformat(),
                    "last_pause_kind": "morning_floor",
                    "pause_soc": 55.0,
                }
            },
            "live_soc": 88.0,
            "morning_floor_threatened": True,
            "priority_runtime_plan": {
                "by_ip": {
                    "192.168.1.153": {
                        "role": "spill",
                        "prefer_on": False,
                    }
                }
            },
            "fill_schedule": {
                "ok": True,
                "soc_percent": 88.0,
                "flex": {
                    "should_start_now": True,
                    "overflow_kwh": 20.0,
                    "ip": "192.168.1.153",
                },
                "flex_stack": [{"ip": "192.168.1.153"}],
                "excess_charge_kw": 30.0,
                "hours_to_max": 0.5,
            },
        }
        actions = [
            {
                "ip": "192.168.1.153",
                "action": "resume",
                "reason": "Flex-Stack gegen Max-SoC: kumuliert 3.2 kW",
                "source": "fill_schedule",
                "fill_urgency": True,
                "urgent": True,
            }
        ]
        out, msgs = apply_switch_commitment_guards(
            actions,
            state,
            {
                "max_device_off_per_day": 3,
                "fill_commitment_override_hours_to_max": 2.0,
                "fill_commitment_override_excess_kw": 5.0,
                "max_soc_percent": 95.0,
            },
            live={},
            now=now,
        )
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0].get("commitment_fill_override"))
        self.assertIsNone(
            state["device_runtime"]["192.168.1.153"].get("committed_off_until")
        )
        self.assertTrue(any("Max-SoC-Füll" in m for m in msgs))

    def test_fill_blocked_at_low_soc_without_fill_urgency(self):
        """Floor-Off bei niedrigem SoC ohne Fill-Urgency bleibt blockiert."""
        now = datetime.now()
        until = (now + timedelta(hours=2)).isoformat()
        state = {
            "device_runtime": {
                "192.168.1.153": {
                    "committed_off_until": until,
                    "paused_at": now.isoformat(),
                    "last_pause_kind": "morning_floor",
                    "pause_soc": 48.0,
                }
            },
            "live_soc": 45.0,
            "morning_floor_threatened": True,
            "fill_schedule": {
                "ok": True,
                "soc_percent": 45.0,
                "flex": {"should_start_now": False, "overflow_kwh": 0},
                "excess_charge_kw": 0.2,
                "hours_to_max": 6.0,
            },
        }
        actions = [
            {
                "ip": "192.168.1.153",
                "action": "resume",
                "reason": "Spätstart-Entladen, Priorität 1",
                "urgent": True,
            }
        ]
        out, msgs = apply_switch_commitment_guards(
            actions,
            state,
            {
                "max_device_off_per_day": 3,
                "fill_commitment_override_hours_to_max": 2.0,
                "fill_commitment_override_excess_kw": 5.0,
                "max_soc_percent": 95.0,
            },
            live={},
            now=now,
        )
        self.assertEqual(out, [])
        self.assertTrue(any("Commitment aus" in m for m in msgs))
        self.assertIsNotNone(
            state["device_runtime"]["192.168.1.153"].get("committed_off_until")
        )

    def test_soc_delta_since_pause_invalidates_committed_off(self):
        """SoC seit Pause +8 %-Pkt → Aus-Commitment ungültig (Fill-Resume)."""
        now = datetime.now()
        until = (now + timedelta(hours=2)).isoformat()
        state = {
            "device_runtime": {
                "192.0.2.10": {
                    "committed_off_until": until,
                    "paused_at": now.isoformat(),
                    "last_pause_kind": "budget_shed",
                    "pause_soc": 59.0,
                }
            },
            "live_soc": 86.0,
            "morning_floor_threatened": True,
            "priority_runtime_plan": {
                "by_ip": {
                    "192.0.2.10": {"role": "anchor", "prefer_on": True},
                }
            },
            "fill_schedule": {
                "ok": True,
                "soc_percent": 86.0,
                "flex": {
                    "should_start_now": True,
                    "overflow_kwh": 0.1,
                    "ip": "192.0.2.10",
                },
                "flex_stack": [{"ip": "192.0.2.10"}],
                # Unter hours/excess-Schwelle – Preemption nur über SoC-Delta
                "excess_charge_kw": 1.0,
                "hours_to_max": 5.0,
            },
        }
        actions = [
            {
                "ip": "192.0.2.10",
                "action": "resume",
                "reason": "Flache Füllkurve: Anker zuerst",
                "source": "fill_schedule",
                "fill_urgency": True,
                "urgent": True,
            }
        ]
        out, msgs = apply_switch_commitment_guards(
            actions,
            state,
            {
                "max_device_off_per_day": 3,
                "fill_commitment_override_hours_to_max": 2.0,
                "fill_commitment_override_excess_kw": 5.0,
                "max_soc_percent": 95.0,
            },
            live={},
            now=now,
        )
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0].get("commitment_fill_override"))
        self.assertIsNone(
            state["device_runtime"]["192.0.2.10"].get("committed_off_until")
        )

    def test_pause_sets_last_pause_kind_morning_floor(self):
        from control_algorithm.switch_commitment import update_runtime_commitments

        now = datetime.now()
        state = {"device_runtime": {}, "live_soc": 52.0}
        state = update_runtime_commitments(
            state,
            [
                {
                    "ip": "192.168.1.153",
                    "action": "pause",
                    "morning_floor": True,
                    "budget_shed": True,
                    "reason": "Morgen-Floor (≥46 %): aus",
                }
            ],
            {"switch_commitment_off_hours": 2.0, "min_device_runtime_hours": 3.0},
            now=now,
        )
        entry = state["device_runtime"]["192.168.1.153"]
        self.assertEqual(entry["last_pause_kind"], "morning_floor")
        self.assertIsNotNone(entry.get("committed_off_until"))
        self.assertEqual(entry.get("pause_soc"), 52.0)

    def test_anker_reserve_pause_classified_budget_shed_with_pause_soc(self):
        from control_algorithm.switch_commitment import (
            pause_kind_from_action,
            update_runtime_commitments,
        )

        act = {
            "ip": "192.0.2.10",
            "action": "pause",
            "reason": (
                "Anker-Reserve erreicht: SoC 59.0 % – Rest 0.0 kWh < 0,75×5.81 kW"
            ),
        }
        self.assertEqual(pause_kind_from_action(act), "budget_shed")
        now = datetime.now()
        state = update_runtime_commitments(
            {"device_runtime": {}, "live_soc": 59.0},
            [act],
            {"switch_commitment_off_hours": 2.0, "min_device_runtime_hours": 1.0},
            now=now,
        )
        entry = state["device_runtime"]["192.0.2.10"]
        self.assertEqual(entry["last_pause_kind"], "budget_shed")
        self.assertEqual(entry.get("pause_soc"), 59.0)

    def test_priority_enforce_does_not_undo_floor_shed(self):
        from control_algorithm.load_scheduler import _enforce_priority_before_switch

        now = datetime.now()
        until = (now + timedelta(hours=2)).isoformat()
        devices = [
            {"ip": "192.168.1.140", "priority": 6, "power_kw": 5.5, "enabled": True},
            {"ip": "192.168.1.153", "priority": 0.5, "power_kw": 3.2, "enabled": True},
        ]
        state = {
            "device_runtime": {
                "192.168.1.140": {
                    "committed_off_until": until,
                    "last_pause_kind": "morning_floor",
                    "paused_at": now.isoformat(),
                    "pause_soc": 50.0,
                }
            },
            "live_soc": 52.0,
            # Keine Fill-Urgency → Anti-Flatter hält Floor-Shed
            "fill_schedule": {
                "ok": True,
                "flex": {"should_start_now": False, "overflow_kwh": 0},
                "excess_charge_kw": 0.2,
                "hours_to_max": 8.0,
            },
        }
        actions = [
            {
                "ip": "192.168.1.153",
                "action": "resume",
                "reason": "Flex-Stack gegen Max-SoC",
                "source": "fill_schedule",
                "fill_urgency": True,
            }
        ]
        out, sim, load, msgs = _enforce_priority_before_switch(
            actions,
            devices,
            {"192.168.1.140": "paused", "192.168.1.153": "paused"},
            0.0,
            live_status={"192.168.1.140": "paused", "192.168.1.153": "paused"},
            state=state,
            config={"max_soc_percent": 95.0},
            live={"einspeisung_kw": 0.0},
        )
        self.assertEqual(out, [])
        self.assertTrue(any("Anti-Flatter" in m for m in msgs))
        self.assertEqual(sim.get("192.168.1.140"), "paused")
        self.assertEqual(sim.get("192.168.1.153"), "paused")

class TestHardDailyOnLimit(unittest.TestCase):
    def test_fourth_resume_blocked_until_eod(self):
        now = datetime(2026, 8, 20, 15, 0, 0)
        state = {
            "device_runtime": {
                "10.0.0.9": {
                    "on_count_today": 3,
                    "on_count_date": "2026-08-20",
                    "off_count_today": 2,
                    "off_count_date": "2026-08-20",
                }
            }
        }
        actions = [
            {
                "ip": "10.0.0.9",
                "action": "resume",
                "reason": "Netzeinspeisung 4.0 kW vermeiden",
            }
        ]
        out, msgs = apply_switch_commitment_guards(
            actions,
            state,
            {
                "max_device_on_per_day": 3,
                "hard_daily_on_limit": True,
                "max_device_off_per_day": 3,
            },
            live={"einspeisung_kw": 4.0},
            now=now,
        )
        self.assertEqual(out, [])
        self.assertTrue(any("Tages-Einschaltlimit" in m for m in msgs))
        self.assertGreaterEqual(
            int((state.get("switch_risk") or {}).get("blocked_resumes") or 0), 1
        )

    def test_emergency_resume_still_allowed(self):
        now = datetime(2026, 8, 20, 15, 0, 0)
        state = {
            "device_runtime": {
                "10.0.0.9": {
                    "on_count_today": 3,
                    "on_count_date": "2026-08-20",
                }
            }
        }
        actions = [
            {
                "ip": "10.0.0.9",
                "action": "resume",
                "reason": "PV-Sicherheit: Unterproduktion",
                "source": "pv_safety",
                "emergency": True,
            }
        ]
        out, _ = apply_switch_commitment_guards(
            actions,
            state,
            {"max_device_on_per_day": 3, "hard_daily_on_limit": True},
            live={},
            now=now,
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["action"], "resume")

    def test_on_count_increments_on_resume(self):
        now = datetime(2026, 8, 20, 12, 0, 0)
        state = {
            "device_runtime": {
                "10.0.0.9": {
                    "on_count_today": 2,
                    "on_count_date": "2026-08-20",
                }
            }
        }
        update_runtime_commitments(
            state,
            [{"ip": "10.0.0.9", "action": "resume", "reason": "Export"}],
            {"max_device_on_per_day": 3, "min_device_runtime_hours": 1.0},
            now=now,
        )
        entry = state["device_runtime"]["10.0.0.9"]
        self.assertEqual(entry["on_count_today"], 3)
        self.assertTrue(entry.get("on_limit_locked"))

    def test_pause_avoided_when_on_limit_reached(self):
        now = datetime(2026, 8, 20, 16, 0, 0)
        state = {
            "device_runtime": {
                "10.0.0.9": {
                    "on_count_today": 3,
                    "on_count_date": "2026-08-20",
                    "resumed_at": (now - timedelta(hours=4)).isoformat(),
                }
            }
        }
        actions = [
            {
                "ip": "10.0.0.9",
                "action": "pause",
                "reason": "Energie-Budget: Last zu hoch",
            }
        ]
        out, msgs = apply_switch_commitment_guards(
            actions,
            state,
            {"max_device_on_per_day": 3, "hard_daily_on_limit": True},
            live={},
            now=now,
        )
        self.assertEqual(out, [])
        self.assertTrue(any("Tages-Einschaltlimit" in m and "Pause" in m for m in msgs))

    def test_run_reserve_pause_beats_fill_resume_and_on_commitment(self):
        now = datetime(2026, 10, 1, 18, 50, 0)
        actions = [
            {
                "ip": "192.0.2.10",
                "action": "resume",
                "reason": "Flache Füllkurve: Anker zuerst – Soll 0.0 kW, Ist 0.0 kW",
                "source": "fill_schedule",
                "fill_urgency": True,
                "urgent": True,
            },
            {
                "ip": "192.0.2.10",
                "action": "pause",
                "reason": "Laufreserve -154.1 kWh – Speicher trägt nicht bis zum PV-Break-even",
                "run_reserve": True,
            },
        ]
        state = {
            "device_runtime": {
                "192.0.2.10": {
                    "committed_on_until": datetime(2026, 10, 2, 12, 12, 0).isoformat(),
                    "resumed_at": now.isoformat(),
                }
            }
        }
        out, msgs = apply_switch_commitment_guards(
            actions, state, {"max_device_off_per_day": 3}, live={"pv_kw": 0.0}, now=now
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["action"], "pause")
        self.assertTrue(out[0].get("run_reserve"))


if __name__ == "__main__":
    unittest.main()
