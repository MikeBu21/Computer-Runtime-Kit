# -*- coding: utf-8 -*-
"""Wochen-Algo nutzt Speicher-Band-Decke/Floor (nicht hartes 95 %)."""
from __future__ import annotations

import unittest
from datetime import datetime
from unittest.mock import patch

from control_algorithm.energy_balance import (
    _max_miner_kw_for_horizon,
    _planning_band_caps,
    build_weekly_energy_plan,
    compute_24h_absorption_plan,
    simulate_battery_horizon,
)
from control_algorithm.steering_learning import ensure_steering_learning, get_steering_params
from control_algorithm.storage_band_learning import band_consumption_plan_scale


def _cfg(**kw) -> dict:
    base = {
        "has_storage": True,
        "storage_kwh": 200.0,
        "min_soc_percent": 35.0,
        "morning_soc_percent": 46.0,
        "max_soc_percent": 95.0,
        "evening_soc_percent": 90.0,
        "pv_kwp": 190.0,
        "devices": [{"id": "m1", "enabled": True, "power_kw": 5.0}],
    }
    base.update(kw)
    return base


def _overfill_state() -> dict:
    state = {
        "pv_yield_today_kwh": 700,
        "pv_yield_tomorrow_kwh": 700,
        "pv_yield_day_after_kwh": 700,
        "consumption_estimates": {
            "base_only": {"by_day_kwh": [350, 350, 350]},
        },
        "pv_forecast_by_date": {},
    }
    ensure_steering_learning(state)
    params = state["steering_learning"]["params"]
    params["band_max_target_pct"] = 88.0
    params["band_weather_guard"] = 0.55
    params["band_horizon_weight"] = 1.0
    return state


def _underfill_state() -> dict:
    state = {
        "pv_yield_today_kwh": 80,
        "pv_yield_tomorrow_kwh": 60,
        "pv_yield_day_after_kwh": 50,
        "consumption_estimates": {
            "base_only": {"by_day_kwh": [520, 530, 540]},
        },
        "consumption_pattern_estimate": {"delta_vs_forecast_pct": 25},
    }
    ensure_steering_learning(state)
    params = state["steering_learning"]["params"]
    params["band_min_cushion_pct"] = 8.0
    params["band_consumption_guard"] = 0.6
    params["band_horizon_weight"] = 1.0
    return state


class TestStorageBandWeekAlgo(unittest.TestCase):
    def test_planning_caps_overfill_below_hard_max(self):
        cfg = _cfg()
        state = _overfill_state()
        live = {"soc_percent": 92.0}
        caps = _planning_band_caps(cfg, state, live=live)
        self.assertLess(caps["plan_max"], 95.0)
        self.assertLessEqual(caps["plan_max"], 92.0)
        self.assertGreater(caps["overfill_risk"], 0.15)
        self.assertLess(caps["pv_scale"], 1.0)

    def test_horizon_sim_uses_plan_max_not_95(self):
        cfg = _cfg()
        state = _overfill_state()
        now = datetime(2026, 6, 15, 12, 0, 0)
        with patch(
            "control_algorithm.energy_balance.planned_base_consumption_kwh",
            return_value=0.2,
        ), patch(
            "control_algorithm.energy_balance.expected_hydro_production_kwh",
            return_value=0.0,
        ), patch(
            "control_algorithm.energy_balance._pv_forecast_kwh",
            return_value=2.0,
        ):
            sim = simulate_battery_horizon(
                cfg,
                state,
                "/tmp",
                now,
                start_kwh=180.0,
                miner_kw=0.0,
                horizon_days=1,
            )
        self.assertLess(float(sim["max_soc_percent"]), 95.0)
        self.assertEqual(float(sim["hard_max_soc_percent"]), 95.0)
        self.assertLessEqual(float(sim["max_soc_percent"]), 92.0)

    def test_weekly_plan_exposes_band_fields_and_caps_soc(self):
        cfg = _cfg()
        state = _overfill_state()
        today = datetime(2026, 6, 15, 10, 0, 0)
        hours = {f"h{h:02d}": 8.0 for h in range(8, 18)}
        state["pv_forecast_by_date"] = {today.date().isoformat(): hours}
        state["horizon_projection"] = {
            "miner_budget_kw": 0.0,
            "risk_factor_percent": 100,
            "weather_context": {"days": [{"offset": i} for i in range(7)]},
        }
        live = {"soc_percent": 85.0, "pv_kw": 0}
        # Patch nur, falls die Helfer existieren; sonst echte Pfade.
        patches = []
        import control_algorithm.energy_balance as eb
        if hasattr(eb, "weekday_base_consumption_detail"):
            patches.append(patch(
                "control_algorithm.energy_balance.weekday_base_consumption_detail",
                return_value={"total_base_kwh": 10.0},
            ))
        if hasattr(eb, "_hourly_profile_for_date"):
            patches.append(patch(
                "control_algorithm.energy_balance._hourly_profile_for_date",
                return_value=([0.3] * 24, [0.0] * 24),
            ))
        for name, ret in (
            ("get_hourly_actual_soc_today", [None] * 24),
            ("weather_context", {"days": [{"offset": i} for i in range(7)]}),
        ):
            if hasattr(eb, name):
                patches.append(patch(f"control_algorithm.energy_balance.{name}", return_value=ret))
        for p in patches:
            p.start()
        try:
            plan = build_weekly_energy_plan(cfg, state, live, "/tmp", today)
        finally:
            for p in patches:
                p.stop()
        self.assertIn("plan_max_soc_percent", plan)
        self.assertLess(float(plan["plan_max_soc_percent"]), 95.0)
        self.assertEqual(float(plan["hard_max_soc_percent"]), 95.0)
        plan_max = float(plan["plan_max_soc_percent"])
        for day in plan.get("days") or []:
            for h in day.get("hours") or []:
                soc = h.get("soc_projected_pct")
                if soc is None:
                    soc = h.get("soc_projected_percent")
                if soc is None:
                    soc = h.get("soc_projected_pct")
                if soc is not None:
                    self.assertLessEqual(float(soc), plan_max + 0.15)

    def test_underfill_raises_floor_and_consumption_scale(self):
        cfg = _cfg()
        state = _underfill_state()
        live = {"soc_percent": 50.0}
        caps = _planning_band_caps(cfg, state, live=live)
        self.assertGreater(caps["plan_min"], 46.0)
        self.assertGreater(caps["underfill_risk"], 0.1)
        scale = band_consumption_plan_scale(cfg, state, live=live)
        self.assertGreater(scale, 1.0)

    def test_miner_budget_respects_band_ceiling_meta(self):
        """Gleiche Energiebilanz, nur Band-Decke unterschiedlich."""
        cfg = _cfg()
        now = datetime(2026, 6, 15, 14, 0, 0)
        live = {"soc_percent": 70.0}
        base_energy = {
            "pv_yield_today_kwh": 400,
            "pv_yield_tomorrow_kwh": 400,
            "pv_yield_day_after_kwh": 400,
            "consumption_estimates": {"base_only": {"by_day_kwh": [420, 420, 420]}},
        }

        state_loose = dict(base_energy)
        ensure_steering_learning(state_loose)
        state_loose["steering_learning"]["params"]["band_max_target_pct"] = 94.0
        state_loose["steering_learning"]["params"]["band_weather_guard"] = 0.0
        state_loose["steering_learning"]["params"]["band_horizon_weight"] = 0.0

        state_tight = dict(base_energy)
        ensure_steering_learning(state_tight)
        state_tight["steering_learning"]["params"]["band_max_target_pct"] = 86.0
        state_tight["steering_learning"]["params"]["band_weather_guard"] = 0.0
        state_tight["steering_learning"]["params"]["band_horizon_weight"] = 0.0

        with patch(
            "control_algorithm.energy_balance.planned_base_consumption_kwh",
            return_value=0.5,
        ), patch(
            "control_algorithm.energy_balance.expected_hydro_production_kwh",
            return_value=0.0,
        ), patch(
            "control_algorithm.energy_balance._pv_forecast_kwh",
            return_value=0.8,
        ), patch(
            "control_algorithm.energy_balance.weather_context",
            return_value={"days": [{"steering_mode": "normal"}]},
        ), patch(
            "control_algorithm.energy_balance._proactive_discharge_kw",
            return_value=None,
        ), patch(
            "control_algorithm.energy_balance._export_avoidance_kw",
            return_value=None,
        ), patch(
            "control_algorithm.energy_balance._near_term_discharge_boost",
            return_value=(None, None, {}),
        ):
            kw_loose, _, meta_l = _max_miner_kw_for_horizon(
                cfg, state_loose, live, "/tmp", now, horizon_days=3
            )
            kw_tight, _, meta_t = _max_miner_kw_for_horizon(
                cfg, state_tight, live, "/tmp", now, horizon_days=3
            )
        self.assertLess(
            float(meta_t.get("plan_max_soc_percent") or 95),
            float(meta_l.get("plan_max_soc_percent") or 95),
        )
        self.assertLessEqual(kw_tight, kw_loose + 0.05)

    def test_absorption_headroom_uses_plan_max(self):
        cfg = _cfg()
        state = _overfill_state()
        live = {"soc_percent": 86.0}
        caps = _planning_band_caps(cfg, state, live=live)
        plan = compute_24h_absorption_plan(cfg, state, live, week_plan={"days": []})
        self.assertAlmostEqual(
            float(plan["plan_max_soc_percent"]), float(caps["plan_max"]), places=1
        )
        expected_hr = max(0.0, (caps["plan_max"] - 86.0) / 100.0 * cfg["storage_kwh"])
        self.assertAlmostEqual(float(plan["headroom_kwh"]), expected_hr, places=1)

    def test_curve_plan_peak_uses_band_ceiling(self):
        from control_algorithm.curve_plan import compute_target_soc_curve

        cfg = _cfg()
        state = _overfill_state()
        live = {"soc_percent": 70.0, "battery_power_kw": 0.0}
        curve = compute_target_soc_curve(
            cfg, state, live, now=datetime(2026, 6, 15, 10, 0)
        )
        self.assertTrue(curve.get("ok"))
        self.assertLess(float(curve["max_soc_percent"]), 95.0)
        self.assertEqual(float(curve["hard_max_soc_percent"]), 95.0)


if __name__ == "__main__":
    unittest.main()
