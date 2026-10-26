# -*- coding: utf-8 -*-
"""Tests für das Anker-Fenster-Neuron."""
import os
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from control_algorithm.anchor_window import (  # noqa: E402
    anchor_device,
    apply_anchor_window_guards,
    blocks_flex_resume,
    committed_window,
    device_power_kw,
    mark_window_started,
    shorten_window_to,
    window_active_now,
)

ANCHOR = "10.0.0.10"
FLEX = "10.0.0.20"


def _config(**over):
    cfg = {
        "anchor_window_enabled": True,
        "anchor_window_shadow_days": 14,
        "anchor_window_min_hours": 2.0,
        "anchor_window_earliest_hour": 4,
        "storage_kwh": 41.0,
        "min_soc_percent": 30.0,
        "max_soc_percent": 95.0,
        "devices": [
            {"ip": ANCHOR, "enabled": True, "power_kw": 6.12, "priority": 10.0},
            {"ip": FLEX, "enabled": True, "power_kw": 3.36, "priority": 0.5},
        ],
    }
    cfg.update(over)
    return cfg


def _state_with_window(now, *, hours=6.0, shadow=False, started=True, spill=0.0):
    """State mit einem bereits laufenden, festgeschriebenen Fenster."""
    t_on = now - timedelta(hours=1)
    t_off = t_on + timedelta(hours=hours)
    return {
        "anchor_window": {
            "ok": True,
            "anchor_ip": ANCHOR,
            "anchor_kw": 6.12,
            "hours": hours,
            "shadow": shadow,
            "shadow_days_remaining": 14 if shadow else 0,
            "min_soc_projected": 41.0,
            "spill_kwh": spill,
            "flex_spill_allowed": spill > 0.3,
            "reason": "Test",
        },
        "anchor_window_committed": {
            "date": now.date().isoformat(),
            "t_on": t_on.isoformat(timespec="seconds"),
            "t_off": t_off.isoformat(timespec="seconds"),
            "started": started,
            "anchor_ip": ANCHOR,
        },
    }


class TestAnchorSelection(unittest.TestCase):
    def test_highest_priority_wins(self):
        dev = anchor_device(_config())
        self.assertEqual(dev["ip"], ANCHOR)

    def test_measured_power_preferred(self):
        self.assertEqual(device_power_kw({"power_kw": 6.12, "measured_power_kw": 5.79}), 5.79)
        self.assertEqual(device_power_kw({"power_kw": 6.12}), 6.12)
        self.assertEqual(device_power_kw(None), 0.0)


class TestWindowGuards(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 6, 12, 0)
        self.cfg = _config()
        self.live = {"soc_percent": 60.0, "miner_status": {ANCHOR: "running"}}

    def test_window_is_active(self):
        state = _state_with_window(self.now)
        self.assertTrue(window_active_now(state, now=self.now))

    def test_weather_pause_on_anchor_is_dropped(self):
        state = _state_with_window(self.now)
        actions = [
            {
                "ip": ANCHOR,
                "action": "pause",
                "reason": "PV-Sicherheitsabschaltung: Pace -22.0 %",
                "source": "pv_safety",
            }
        ]
        out, msgs = apply_anchor_window_guards(
            actions, state, self.cfg, live=self.live, now=self.now
        )
        self.assertEqual(out, [])
        self.assertTrue(any("Pause verworfen" in m for m in msgs))

    def test_budget_pause_on_anchor_is_dropped(self):
        state = _state_with_window(self.now)
        actions = [
            {
                "ip": ANCHOR,
                "action": "pause",
                "reason": "Anker-Reserve / Budget (0.8 kW): Last 6.1 kW",
                "budget_shed": True,
            }
        ]
        out, _ = apply_anchor_window_guards(
            actions, state, self.cfg, live=self.live, now=self.now
        )
        self.assertEqual(out, [])

    def test_min_soc_emergency_still_pauses_anchor(self):
        state = _state_with_window(self.now)
        actions = [
            {
                "ip": ANCHOR,
                "action": "pause",
                "reason": "Mindest-SoC (30%) unterschritten (aktuell 28%)",
                "source": "min_soc",
                "emergency": True,
                "force": True,
            }
        ]
        out, _ = apply_anchor_window_guards(
            actions, state, self.cfg, live=self.live, now=self.now
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["action"], "pause")

    def test_min_soc_lock_disables_window(self):
        state = _state_with_window(self.now)
        state["min_soc_lock_active"] = True
        actions = [{"ip": ANCHOR, "action": "pause", "reason": "Budget"}]
        out, _ = apply_anchor_window_guards(
            actions, state, self.cfg, live=self.live, now=self.now
        )
        self.assertEqual(out, actions)

    def test_night_lock_ends_window_instead_of_breaking_it(self):
        state = _state_with_window(self.now)
        state["night_lock_active"] = True
        actions = [
            {
                "ip": ANCHOR,
                "action": "pause",
                "reason": "Nacht-Sperre: Min-SoC-Gefahr",
                "source": "night_lock",
            }
        ]
        out, msgs = apply_anchor_window_guards(
            actions, state, self.cfg, live=self.live, now=self.now
        )
        self.assertEqual(len(out), 1)
        self.assertTrue(any("planmässig" in m for m in msgs))
        self.assertFalse(window_active_now(state, now=self.now + timedelta(minutes=1)))

    def test_anchor_resumed_when_missing_in_window(self):
        state = _state_with_window(self.now)
        live = {"soc_percent": 60.0, "miner_status": {ANCHOR: "stopped"}}
        out, _ = apply_anchor_window_guards([], state, self.cfg, live=live, now=self.now)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["action"], "resume")
        self.assertEqual(out[0]["source"], "anchor_window")

    def test_no_resume_while_pv_safety_active(self):
        state = _state_with_window(self.now)
        state["pv_safety_active"] = True
        live = {"soc_percent": 60.0, "miner_status": {ANCHOR: "stopped"}}
        out, _ = apply_anchor_window_guards([], state, self.cfg, live=live, now=self.now)
        self.assertEqual(out, [])


class TestFlexHandling(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 6, 12, 0)
        self.cfg = _config()
        self.live = {"soc_percent": 60.0, "miner_status": {ANCHOR: "running"}}

    def test_flex_blocked_without_spill(self):
        state = _state_with_window(self.now, spill=0.0)
        self.assertTrue(blocks_flex_resume(self.cfg, state, FLEX, now=self.now))
        out, _ = apply_anchor_window_guards(
            [{"ip": FLEX, "action": "resume", "reason": "Füllkurve"}],
            state,
            self.cfg,
            live=self.live,
            now=self.now,
        )
        self.assertEqual(out, [])

    def test_flex_allowed_with_spill(self):
        state = _state_with_window(self.now, spill=4.2)
        self.assertFalse(blocks_flex_resume(self.cfg, state, FLEX, now=self.now))
        out, _ = apply_anchor_window_guards(
            [{"ip": FLEX, "action": "resume", "reason": "Flex-Stack gegen Max-SoC"}],
            state,
            self.cfg,
            live=self.live,
            now=self.now,
        )
        self.assertEqual(len(out), 1)

    def test_flex_allowed_under_max_soc_protection(self):
        state = _state_with_window(self.now, spill=0.0)
        state["max_soc_protection_active"] = True
        self.assertFalse(blocks_flex_resume(self.cfg, state, FLEX, now=self.now))

    def test_flex_pause_untouched(self):
        state = _state_with_window(self.now)
        actions = [{"ip": FLEX, "action": "pause", "reason": "Budget"}]
        out, _ = apply_anchor_window_guards(
            actions, state, self.cfg, live=self.live, now=self.now
        )
        self.assertEqual(out, actions)


class TestShadowMode(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 6, 12, 0)
        self.cfg = _config()

    def test_shadow_changes_nothing_but_logs(self):
        state = _state_with_window(self.now, shadow=True)
        actions = [
            {"ip": ANCHOR, "action": "pause", "reason": "Budget", "budget_shed": True}
        ]
        out, msgs = apply_anchor_window_guards(
            actions, state, self.cfg, live={"miner_status": {}}, now=self.now
        )
        self.assertEqual(out, actions)
        self.assertTrue(any("Schatten" in m for m in msgs))
        self.assertTrue(state["anchor_window"].get("would_block"))

    def test_shadow_logs_flex_block(self):
        state = _state_with_window(self.now, shadow=True, spill=0.0)
        actions = [{"ip": FLEX, "action": "resume", "reason": "Füllkurve"}]
        out, _ = apply_anchor_window_guards(
            actions,
            state,
            self.cfg,
            live={"miner_status": {ANCHOR: "running"}},
            now=self.now,
        )
        self.assertEqual(out, actions)
        blocked = state["anchor_window"].get("would_block") or []
        self.assertTrue(any(FLEX in b for b in blocked))

    def test_shadow_logs_would_resume_anchor(self):
        state = _state_with_window(self.now, shadow=True)
        out, _ = apply_anchor_window_guards(
            [], state, self.cfg, live={"miner_status": {ANCHOR: "stopped"}}, now=self.now
        )
        self.assertEqual(out, [])
        blocked = state["anchor_window"].get("would_block") or []
        self.assertTrue(any("einschalten" in b for b in blocked))


class TestCommitStability(unittest.TestCase):
    def test_shorten_moves_end_earlier_only(self):
        now = datetime(2026, 9, 6, 12, 0)
        state = _state_with_window(now, hours=6.0)
        _, t_off_before, _ = committed_window(state, now)
        shorten_window_to(state, now + timedelta(hours=1), "Test", now=now)
        _, t_off_after, _ = committed_window(state, now)
        self.assertLess(t_off_after, t_off_before)
        shorten_window_to(state, now + timedelta(hours=10), "Zu spät", now=now)
        _, t_off_final, _ = committed_window(state, now)
        self.assertEqual(t_off_final, t_off_after)

    def test_closed_window_does_not_reopen_same_day(self):
        from control_algorithm.anchor_window import _commit_window

        now = datetime(2026, 9, 6, 12, 0)
        state = _state_with_window(now, hours=6.0, started=True)
        shorten_window_to(state, now, "Nacht-Sperre", now=now)
        self.assertTrue(state["anchor_window_committed"]["closed"])
        # Neuer, wieder zulässiger Plan darf den Block nicht erneut öffnen
        plan = {
            "ok": True,
            "anchor_ip": ANCHOR,
            "hours": 5.0,
            "t_on": (now + timedelta(hours=1)).isoformat(timespec="seconds"),
            "t_off": (now + timedelta(hours=6)).isoformat(timespec="seconds"),
        }
        _commit_window(state, plan, now + timedelta(hours=1))
        self.assertFalse(window_active_now(state, now=now + timedelta(hours=2)))

    def test_mark_started_freezes_start(self):
        now = datetime(2026, 9, 6, 12, 0)
        state = _state_with_window(now, started=False)
        mark_window_started(state, now)
        t_on, _, _ = committed_window(state, now)
        self.assertEqual(t_on, now)
        self.assertTrue(state["anchor_window_committed"]["started"])


class TestEveningLearning(unittest.TestCase):
    def _state(self, **today):
        from control_algorithm.steering_learning import ensure_steering_learning

        state = {}
        sl = ensure_steering_learning(state)
        sl["today"].update(today)
        return state

    def test_soc_violation_increases_safety_margin(self):
        from control_algorithm.steering_learning import (
            anchor_window_safety_pct,
            run_anchor_window_evening_learning,
        )

        state = self._state(
            aw_window_ticks=100,
            aw_running_ticks=100,
            aw_soc_violation_ticks=4,
            aw_headroom_sum=200.0,
            aw_headroom_samples=100,
        )
        before = anchor_window_safety_pct(state)
        run_anchor_window_evening_learning(state, _config())
        self.assertGreater(anchor_window_safety_pct(state), before)

    def test_clean_hold_with_headroom_relaxes(self):
        from control_algorithm.steering_learning import (
            anchor_window_early_start_bias,
            anchor_window_safety_pct,
            run_anchor_window_evening_learning,
        )

        state = self._state(
            aw_window_ticks=100,
            aw_running_ticks=100,
            aw_break_ticks=0,
            aw_soc_violation_ticks=0,
            aw_headroom_sum=2500.0,
            aw_headroom_samples=100,
        )
        safety_before = anchor_window_safety_pct(state)
        bias_before = anchor_window_early_start_bias(state)
        run_anchor_window_evening_learning(state, _config())
        self.assertLess(anchor_window_safety_pct(state), safety_before)
        self.assertGreater(anchor_window_early_start_bias(state), bias_before)

    def test_too_few_samples_no_change(self):
        from control_algorithm.steering_learning import (
            anchor_window_safety_pct,
            run_anchor_window_evening_learning,
        )

        state = self._state(aw_window_ticks=2, aw_soc_violation_ticks=1)
        before = anchor_window_safety_pct(state)
        run_anchor_window_evening_learning(state, _config())
        self.assertEqual(anchor_window_safety_pct(state), before)


if __name__ == "__main__":
    unittest.main()
