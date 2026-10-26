# -*- coding: utf-8 -*-
"""Tests Schiedsrichter: balanciertes Cap zwischen Export und Reserve."""
from __future__ import annotations

import unittest

from control_algorithm.decision_arbitrator import (
    arbitrate_load_decisions,
    compute_balanced_load_cap,
    compute_export_absorb_floor,
    export_priority_active,
    reconcile_export_guard,
    reserve_priority_active,
    update_export_guard,
)


class TestBalancedLoadCap(unittest.TestCase):
    def test_pre_storm_charge_caps_to_anchor_without_export(self):
        cap, floor, mode = compute_balanced_load_cap(
            budget_kw=0.0,
            preferred_kw=31.6,
            priority_plan={
                "devices": [
                    {"role": "anchor", "power_kw": 5.5},
                    {"role": "flex", "power_kw": 5.5},
                ],
            },
            export_absorb_floor=0.0,
            reserve_priority=True,
            live_export_kw=0.0,
            learn_thr=0.2,
            sustainable_cap=37.0,
            binding="pre_storm_charge",
        )
        self.assertEqual(floor, 0.0)
        self.assertAlmostEqual(cap, 5.5)
        self.assertEqual(mode, "Schlechtwetter-Vorladung")

    def test_export_absorb_only_stacks_needed_devices(self):
        devices = [
            {"ip": "a", "power_kw": 5.5, "priority": "high"},
            {"ip": "b", "power_kw": 5.5, "priority": "medium"},
            {"ip": "c", "power_kw": 5.5, "priority": "low"},
        ]
        floor = compute_export_absorb_floor(
            devices,
            live_export_kw=0.25,
            learn_thr=0.2,
        )
        self.assertAlmostEqual(floor, 5.5)
        self.assertLess(floor, 16.5)

    def test_reserve_priority_active_for_pre_storm(self):
        self.assertTrue(
            reserve_priority_active(
                binding="pre_storm_charge",
                budget_kw=0.0,
                morning_threat=False,
            )
        )


class TestReconcileExportGuard(unittest.TestCase):
    def test_clears_guard_on_pre_storm_without_export(self):
        state = {
            "export_guard_active": True,
            "export_guard_floor_kw": 31.6,
            "export_guard_reason": "old",
        }
        msgs = reconcile_export_guard(
            state,
            binding="pre_storm_charge",
            live_export_kw=0.0,
            learn_thr=0.2,
            soc=84.0,
            checkpoint_soc=46.0,
            balanced_cap=11.0,
            export_absorb_floor=0.0,
            reserve_priority=True,
        )
        self.assertFalse(state.get("export_guard_active"))
        self.assertTrue(any("Export-Guard aus" in m for m in msgs))

    def test_lowers_sticky_guard_to_balanced_cap(self):
        state = {
            "export_guard_active": True,
            "export_guard_floor_kw": 31.6,
        }
        msgs = reconcile_export_guard(
            state,
            binding="none",
            live_export_kw=0.0,
            learn_thr=0.2,
            soc=84.0,
            checkpoint_soc=46.0,
            balanced_cap=11.0,
            export_absorb_floor=0.0,
            reserve_priority=False,
        )
        self.assertAlmostEqual(state["export_guard_floor_kw"], 11.0)
        self.assertTrue(any("31.6" in m and "11.0" in m for m in msgs))


class TestDecisionArbitrator(unittest.TestCase):
    def test_export_resume_beats_budget_pause_same_ip(self):
        actor1 = [{"ip": "10.0.0.1", "action": "resume", "reason": "Einspeisung vermeiden"}]
        actor2 = [
            {
                "ip": "10.0.0.1",
                "action": "pause",
                "reason": "Energie-Budget · Nacht ohne PV",
                "force": True,
            }
        ]
        actions, msgs = arbitrate_load_decisions(
            actor1_actions=actor1,
            actor2_actions=actor2,
            other_actions=[],
            export_priority=True,
            export_floor_kw=5.5,
            devices_by_ip={"10.0.0.1": {"power_kw": 5.5}},
        )
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["action"], "resume")
        self.assertTrue(any("Vorrang" in m or "Mindest-Last" in m for m in msgs))

    def test_budget_pause_allowed_above_reserve_cap(self):
        actor2 = [
            {
                "ip": "10.0.0.2",
                "action": "pause",
                "reason": "Energie-Budget",
                "force": False,
            }
        ]
        actions, msgs = arbitrate_load_decisions(
            actor1_actions=[],
            actor2_actions=actor2,
            other_actions=[],
            export_priority=True,
            export_floor_kw=0.0,
            devices_by_ip={"10.0.0.2": {"power_kw": 5.5}},
            balanced_cap_kw=11.0,
            reserve_priority=True,
        )
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["action"], "pause")

    def test_budget_pause_blocked_below_floor(self):
        actor2 = [{"ip": "10.0.0.2", "action": "pause", "reason": "Energie-Budget", "force": True}]
        actions, msgs = arbitrate_load_decisions(
            actor1_actions=[],
            actor2_actions=actor2,
            other_actions=[],
            export_priority=True,
            export_floor_kw=11.0,
            devices_by_ip={"10.0.0.2": {"power_kw": 5.5}},
        )
        self.assertEqual(actions, [])
        self.assertTrue(any("Mindest-Last" in m or "Export-Floor" in m for m in msgs))

    def test_without_export_priority_keeps_budget_pause(self):
        actor2 = [{"ip": "10.0.0.2", "action": "pause", "reason": "Budget", "force": True}]
        actions, _msgs = arbitrate_load_decisions(
            actor1_actions=[],
            actor2_actions=actor2,
            other_actions=[],
            export_priority=False,
            export_floor_kw=0,
            devices_by_ip={"10.0.0.2": {"power_kw": 5.5}},
        )
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["action"], "pause")

    def test_export_priority_false_when_pre_storm_no_export(self):
        state = {"export_guard_active": False}
        self.assertFalse(
            export_priority_active(
                discharge_mode=False,
                export_mode=False,
                binding="pre_storm_charge",
                state=state,
                live_export_kw=0.0,
                learn_thr=0.2,
            )
        )


class TestExportGuardLower(unittest.TestCase):
    def test_sticky_max_then_allow_lower(self):
        state: dict = {}
        update_export_guard(
            state,
            active=True,
            floor_kw=31.6,
            reason="export",
            soc=62.0,
            checkpoint_soc=45.0,
        )
        self.assertAlmostEqual(state["export_guard_floor_kw"], 31.6)
        update_export_guard(
            state,
            active=True,
            floor_kw=11.0,
            reason="lower",
            soc=62.0,
            checkpoint_soc=45.0,
            allow_lower=True,
        )
        self.assertAlmostEqual(state["export_guard_floor_kw"], 11.0)

    def test_guard_clears_near_checkpoint(self):
        state: dict = {}
        update_export_guard(
            state,
            active=True,
            floor_kw=20.0,
            reason="test",
            soc=54.0,
            checkpoint_soc=40.0,
        )
        self.assertTrue(
            export_priority_active(
                discharge_mode=False,
                export_mode=False,
                binding="",
                state=state,
            )
        )
        update_export_guard(
            state,
            active=False,
            floor_kw=0,
            reason="",
            soc=41.0,
            checkpoint_soc=40.0,
        )
        self.assertFalse(state.get("export_guard_active"))


if __name__ == "__main__":
    unittest.main()
