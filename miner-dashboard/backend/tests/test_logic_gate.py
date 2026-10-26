# -*- coding: utf-8 -*-
"""Linien im Logikgatter leuchten nur bei aktiviertem Quell-Gatter."""
from __future__ import annotations

from control_algorithm.logic_gate import build_logic_gate


def _by_id(gate):
    return {n["id"]: n for n in gate["nodes"]}


def _edge_map(gate):
    return {(e["from"], e["to"]): e for e in gate["edges"]}


def _screenshot_state():
    return {
        "pv_forecast_override": {
            "active": True,
            "pace_deviation_pct": 15.8,
            "effective_kwh": 40,
        },
        "balance_projection": {
            "miner_budget_kw": 31.6,
            "miner_surplus_kwh": 53.0,
            "risk_factor_percent": 100,
        },
        "curve_plan": {
            "ok": True,
            "steer": "mehr_last_frueher",
            "formula": "Ziel: Last einschalten",
        },
        "pv_safety_active": False,
        "min_soc_lock_active": False,
        "night_lock_active": False,
        "anomaly_active": False,
        "anomaly_low_active": False,
        "export_guard_active": False,
        "pv_yield_today_kwh": 12.0,
        "last_weather_fetch": "2026-08-16T12:00:00",
    }


def _screenshot_live():
    return {"soc_percent": 87.0, "pv_kw": 35.0, "export_kw": 0.0}


def test_paused_oms_and_quiet_watchdog_edges_stay_idle():
    gate = build_logic_gate({}, _screenshot_state(), live=_screenshot_live())
    nodes = _by_id(gate)
    edges = _edge_map(gate)

    assert nodes["wetter_oms"]["route"] == "idle"
    assert nodes["wetter_oms"]["active"] is False
    assert nodes["muster_wetter"]["route"] == "on"
    assert nodes["pv_sicherheit"]["active"] is False
    assert nodes["risk_faktor"]["active"] is False
    assert nodes["batterie_schutz"]["active"] is False

    oms = edges[("wetter_oms", "hub_wetter")]
    assert oms["active"] is False
    assert oms["color"] == "idle"

    safety = edges[("pv_sicherheit", "hub_wetter")]
    assert safety["active"] is False
    assert safety["color"] == "idle"

    muster = edges[("muster_wetter", "hub_wetter")]
    assert muster["active"] is True
    assert muster["color"] == "on"

    risk = edges[("energiebilanz", "risk_faktor")]
    assert risk["active"] is False
    assert risk["color"] == "idle"

    bat = edges[("batterie_schutz", "ueberschuss")]
    assert bat["active"] is False
    assert bat["color"] == "idle"

    live_pv = edges[("live_pv", "hub_live")]
    assert live_pv["active"] is False
    assert live_pv["color"] == "idle"

    export = edges[("netz_export", "hub_live")]
    assert export["active"] is False
    assert export["color"] == "idle"


def test_only_emitting_source_keeps_green_path():
    live = {"soc_percent": 87.0, "pv_kw": 35.0, "export_kw": 1.2, "grid_kw": -1.2}
    gate = build_logic_gate({}, _screenshot_state(), live=live)
    edges = _edge_map(gate)
    linien = edges[("hub_live", "linienalgorithmus")]
    assert linien["active"] is True
    assert linien["color"] == "on"
    hub_w = edges[("hub_wetter", "energiebilanz")]
    assert hub_w["active"] is True
    assert hub_w["color"] == "on"


def test_oms_edge_lights_when_muster_does_not_engage():
    state = _screenshot_state()
    state["pv_forecast_override"] = {"active": False}
    gate = build_logic_gate({}, state, live=_screenshot_live())
    nodes = _by_id(gate)
    edges = _edge_map(gate)
    assert nodes["wetter_oms"]["route"] == "on"
    assert nodes["muster_wetter"]["route"] == "idle"
    oms = edges[("wetter_oms", "hub_wetter")]
    assert oms["active"] is True
    assert oms["color"] == "on"
    muster = edges[("muster_wetter", "hub_wetter")]
    assert muster["active"] is False
    assert muster["color"] == "idle"


def test_consumption_edges_stay_idle_without_anomaly():
    state = _screenshot_state()
    state["consumption_pattern_estimate"] = {"estimated_kwh": 18.0, "summary": "Muster"}
    state["steering_learning"] = {"today": {"miss_ticks": 0}}
    gate = build_logic_gate({}, state, live=_screenshot_live())
    nodes = _by_id(gate)
    edges = _edge_map(gate)

    assert nodes["verbrauch_hist"]["route"] == "idle"
    assert nodes["verbrauch_muster"]["route"] == "idle"
    assert nodes["verbrauch_anomalie_hoch"]["route"] == "idle"
    assert nodes["verbrauch_anomalie_tief"]["route"] == "idle"
    assert nodes["steering_learning"]["route"] == "idle"

    for pair in (
        ("verbrauch_hist", "verbrauch_muster"),
        ("verbrauch_muster", "hub_verbrauch"),
        ("verbrauch_anomalie_hoch", "performance_ratio"),
        ("verbrauch_anomalie_tief", "referenz_anker"),
        ("linienalgorithmus", "steering_learning"),
        ("steering_learning", "dazugelernt"),
        ("abend_kalibrierung", "hub_verbrauch"),
    ):
        e = edges[pair]
        assert e["active"] is False, pair
        assert e["color"] == "idle", pair


def test_edge_color_matches_source_box_never_red_from_green():
    state = _screenshot_state()
    state["anomaly_low_active"] = True
    gate = build_logic_gate({}, state, live=_screenshot_live())
    nodes = _by_id(gate)
    edges = _edge_map(gate)
    assert nodes["hub_verbrauch"]["route"] == "on"
    outgoing = edges[("hub_verbrauch", "energiebilanz")]
    assert outgoing["color"] == "on"
    assert outgoing["color"] != "off"
    for e in gate["edges"]:
        if e.get("color") == "off":
            src = nodes[e["from"]]
            assert src["route"] == "off", (e["from"], e["to"], src["route"])
            assert src["active"] is True
def test_pv_safety_edge_lights_red_when_watchdog_fires():
    state = _screenshot_state()
    state["pv_safety_active"] = True
    gate = build_logic_gate({}, state, live=_screenshot_live())
    nodes = _by_id(gate)
    edges = _edge_map(gate)
    assert nodes["pv_sicherheit"]["route"] == "off"
    safety = edges[("pv_sicherheit", "hub_wetter")]
    assert safety["active"] is True
    assert safety["color"] == "off"


def test_pv_safety_stone_stays_idle_when_disabled():
    state = _screenshot_state()
    state["pv_safety_active"] = True
    gate = build_logic_gate(
        {"pv_safety_enabled": False},
        state,
        live=_screenshot_live(),
    )
    nodes = _by_id(gate)
    assert "pv_sicherheit" in nodes
    assert nodes["pv_sicherheit"]["active"] is False
    assert nodes["pv_sicherheit"]["route"] == "idle"
    assert "deaktiviert" in (nodes["pv_sicherheit"].get("detail") or "")


def test_no_branch_stays_wired_when_surplus_is_yes():
    gate = build_logic_gate({}, _screenshot_state(), live=_screenshot_live())
    nodes = _by_id(gate)
    edges = _edge_map(gate)
    import re

    horiz_q = [
        n for n in gate["nodes"] if re.fullmatch(r"horiz_d\d+", str(n["id"]))
    ]
    assert len(horiz_q) == 7
    assert "partei_1" in nodes
    assert "partei_2" in nodes
    assert nodes["schiedsrichter"]["label"] == "Schiedsrichter (Prioritätsleiter)"
    assert "schlechtwetter_streak" in nodes
    assert "horizon_sim" in nodes
    assert "dynam_soc_ziele" in nodes
    assert "horiz_d1_ja" in nodes
    assert "horiz_d1_nein" in nodes
    assert "horiz_d1_pv" in nodes
    for pair in (
        ("ueberschuss", "wochenplaner"),
        ("wochenplaner", "schlechtwetter_streak"),
        ("schlechtwetter_streak", "horiz_d1"),
        ("wochenplaner", "horiz_d1"),
        ("wochenplaner", "nacht_sperre"),
        ("wochenplaner", "last_reduzieren"),
        ("nacht_sperre", "last_reduzieren"),
        ("last_reduzieren", "partei_2"),
        ("partei_2", "prio_invariante"),
        ("partei_1", "prio_invariante"),
        ("prio_invariante", "schiedsrichter"),
        ("anker_streckung", "partei_1"),
        ("horiz_d1", "horiz_d1_ja"),
        ("horiz_d1", "horiz_d1_nein"),
        ("horiz_d1_ja", "horiz_d2"),
        ("horiz_d1_nein", "horiz_d2"),
        ("horiz_d1_slots", "horiz_d1"),
        ("horiz_d7_ja", "prio_laufzeit"),
        ("horiz_d7_nein", "nacht_sperre"),
    ):
        assert pair in edges, pair
    assert nodes["wochenplaner"]["label"] == "Wochenplaner"
    for i in range(1, 7):
        assert (f"horiz_d{i}_ja", f"horiz_d{i + 1}") in edges
        assert (f"horiz_d{i}_nein", f"horiz_d{i + 1}") in edges


def test_horizon_tight_day_splits_to_shed():
    state = _screenshot_state()
    state["balance_projection"]["miner_budget_kw"] = 0.0
    state["balance_projection"]["miner_surplus_kwh"] = 0.0
    state["balance_projection"]["binding_constraint"] = "survival_mode"
    state["horizon_projection"] = {
        "weather_context": {
            "days": [
                {"offset": 0, "steering_mode": "normal", "weather_tier": "good", "pv_kwh": 40},
                {"offset": 1, "steering_mode": "survival_mode", "weather_tier": "bad", "pv_kwh": 4},
                {"offset": 2, "steering_mode": "survival_mode", "weather_tier": "bad", "pv_kwh": 3},
                {"offset": 3, "steering_mode": "normal", "weather_tier": "normal", "pv_kwh": 20},
                {"offset": 4, "steering_mode": "normal", "weather_tier": "good", "pv_kwh": 30},
                {"offset": 5, "steering_mode": "normal", "weather_tier": "good", "pv_kwh": 28},
                {"offset": 6, "steering_mode": "normal", "weather_tier": "good", "pv_kwh": 25},
            ]
        }
    }
    gate = build_logic_gate({}, state, live=_screenshot_live())
    nodes = _by_id(gate)
    edges = _edge_map(gate)
    assert nodes["horiz_d2"]["route"] == "off"
    assert nodes["horiz_d2"]["active"] is True
    nein = edges[("horiz_d2_nein", "last_reduzieren")]
    assert nein["label"] == "Nein"
    assert nodes["partei_2"]["active"] is True
    assert edges[("partei_2", "prio_invariante")]["from"] == "partei_2"


REQUIRED_DECISION_MODULES = (
    "live_soc",
    "live_pv",
    "live_hydro",
    "live_verbrauch",
    "live_batterie",
    "netz_export",
    "wetter_oms",
    "muster_wetter",
    "wetter_massstab",
    "stundenfaktoren",
    "pv_sicherheit",
    "verbrauch_hist",
    "verbrauch_muster",
    "verbrauch_anomalie_hoch",
    "verbrauch_anomalie_tief",
    "performance_ratio",
    "referenz_anker",
    "abend_kalibrierung",
    "soc_lernen",
    "energiebilanz",
    "risk_faktor",
    "verbrauch_risiko",
    "soc_ziele",
    "linienalgorithmus",
    "autopilot_survival",
    "batterie_schutz",
    "max_soc",
    "ueberschuss",
    "tages_ueberschuss",
    "ueberschuss_session",
    "export_vermeidung",
    "near_term",
    "prio_laufzeit",
    "fuellplan",
    "fuell_dringlichkeit",
    "wetter_durchlauf",
    "spaetstart",
    "anker_streckung",
    "wochenplaner",
    "schlechtwetter_streak",
    "horizon_sim",
    "dynam_soc_ziele",
    "morgen_floor",
    "nacht_sperre",
    "last_reduzieren",
    "partei_1",
    "partei_2",
    "schiedsrichter",
    "prio_invariante",
    "min_runtime",
    "min_off",
    "switch_commitment",
    "hysterese",
    "manual_override",
    "plan_hold",
    "anom_resume_sperre",
    "anker_live",
    "algo_aktiv",
    "sicherheits_direkt",
    "steering_learning",
    "dazugelernt",
    "lernfaktor_wetter_oms",
)


def test_all_decision_modules_are_visible_blocks():
    devices = [
        {
            "ip": "192.168.1.10",
            "name": "S21",
            "running": True,
            "planned_action": "Unverändert",
            "power_kw": 5.5,
            "priority": 10,
            "status": "Läuft",
        }
    ]
    gate = build_logic_gate(
        {}, _screenshot_state(), live=_screenshot_live(), devices=devices
    )
    nodes = _by_id(gate)
    missing = [mid for mid in REQUIRED_DECISION_MODULES if mid not in nodes]
    assert missing == [], missing
    for mid in (
        "live_hydro",
        "live_verbrauch",
        "live_batterie",
        "morgen_floor",
        "max_soc",
        "fuell_dringlichkeit",
        "wetter_durchlauf",
        "prio_invariante",
        "min_off",
        "soc_ziele",
        "verbrauch_risiko",
        "algo_aktiv",
    ):
        outgoing = [e for e in gate["edges"] if e["from"] == mid]
        assert outgoing, mid


def test_morning_floor_and_fill_urgency_have_separate_paths():
    state = _screenshot_state()
    state["morning_floor_threatened"] = True
    state["fill_urgency_active"] = True
    state["balance_projection"]["miner_budget_kw"] = 0.0
    state["balance_projection"]["miner_surplus_kwh"] = 0.0
    gate = build_logic_gate({}, state, live=_screenshot_live())
    nodes = _by_id(gate)
    edges = _edge_map(gate)
    assert nodes["morgen_floor"]["active"] is True
    assert nodes["fuell_dringlichkeit"]["route"] == "on"
    assert ("fuell_dringlichkeit", "morgen_floor") in edges
    assert ("morgen_floor", "last_reduzieren") in edges
    assert ("fuell_dringlichkeit", "partei_1") in edges


def test_horizon_week_tree_has_production_block_per_weekday():
    state = _screenshot_state()
    state["horizon_projection"] = {
        "all_checkpoints_ok": True,
        "weather_context": {
            "bad_streak_days": 0,
            "days": [
                {"offset": i, "steering_mode": "normal", "weather_tier": "good", "pv_kwh": 30 + i}
                for i in range(7)
            ],
        },
        "checkpoints": [{"time": "07:00", "target_soc": 40.0}],
    }
    gate = build_logic_gate({}, state, live=_screenshot_live())
    nodes = _by_id(gate)
    for i in range(1, 8):
        assert f"horiz_d{i}_pv" in nodes
        assert f"horiz_d{i}_ja" in nodes
        assert f"horiz_d{i}_nein" in nodes
        assert "Wie schaut" in nodes[f"horiz_d{i}"]["label"] or "Passt" in nodes[f"horiz_d{i}"]["label"]


def test_safety_fast_path_bypasses_filter_chain():
    state = _screenshot_state()
    state["pv_safety_active"] = True
    state["planned_actions"] = [
        {
            "ip": "192.168.1.10",
            "action": "pause",
            "force": True,
            "emergency": True,
            "source": "pv_safety",
        }
    ]
    gate = build_logic_gate({}, state, live=_screenshot_live())
    nodes = _by_id(gate)
    edges = _edge_map(gate)
    assert nodes["sicherheits_direkt"]["route"] == "off"
    assert ("pv_sicherheit", "sicherheits_direkt") in edges
    assert ("sicherheits_direkt", "algo_aktiv") in edges
    assert edges[("sicherheits_direkt", "algo_aktiv")]["style"] == "dashed"


def test_safety_fast_path_low_anomaly_resume():
    state = _screenshot_state()
    state["anomaly_low_active"] = True
    state["anomaly_deficit_pct"] = 35.0
    state["planned_actions"] = [
        {
            "ip": "192.168.1.10",
            "action": "resume",
            "force": True,
            "emergency": True,
            "source": "anomaly_low",
        }
    ]
    gate = build_logic_gate({}, state, live=_screenshot_live())
    nodes = _by_id(gate)
    edges = _edge_map(gate)
    assert nodes["sicherheits_direkt"]["route"] == "on"
    assert ("verbrauch_anomalie_tief", "sicherheits_direkt") in edges
    assert edges[("verbrauch_anomalie_tief", "sicherheits_direkt")]["color"] == "on"


def test_filter_chain_reaches_devices_via_algo_aktiv():
    state = _screenshot_state()
    state["planned_actions"] = [
        {"ip": "192.168.1.10", "action": "resume", "reason": "test"}
    ]
    devices = [
        {
            "ip": "192.168.1.10",
            "name": "S21",
            "running": False,
            "planned_action": "Einschalten",
            "power_kw": 5.5,
            "priority": 10,
            "priority_label": "10",
            "status": "Aus",
        }
    ]
    gate = build_logic_gate(
        {"enabled": True, "dry_run": True},
        state,
        live=_screenshot_live(),
        devices=devices,
    )
    edges = _edge_map(gate)
    assert ("prio_invariante", "schiedsrichter") in edges
    assert ("schiedsrichter", "plan_hold") in edges
    assert ("plan_hold", "anom_resume_sperre") in edges
    assert ("anom_resume_sperre", "min_runtime") in edges
    assert ("min_runtime", "min_off") in edges
    assert ("switch_limit", "manual_override") in edges
    assert ("manual_override", "hysterese") in edges
    assert ("hysterese", "anker_live") in edges
    assert ("anker_live", "algo_aktiv") in edges
    assert ("algo_aktiv", "dev_192_168_1_10") in edges
    nodes = _by_id(gate)
    assert nodes["algo_aktiv"]["live"]["dry_run"] is True
    assert "manual_override" in nodes
    assert nodes["manual_override"]["info"]["module"] == "manual_override"


def test_logic_gate_layout_overrides():
    state = _screenshot_state()
    state["logic_gate_layout"] = {
        "layout_rev": "2026-08-18-grid-v3",
        "positions": {"wochenplaner": {"x": 999.0, "y": 888.0}},
        "edge_waypoints": {"a->b": {"x": 10.0, "y": 20.0}},
    }
    gate = build_logic_gate({}, state, live=_screenshot_live())
    nodes = _by_id(gate)
    assert nodes["wochenplaner"]["x"] == 999.0
    assert nodes["wochenplaner"]["y"] == 888.0
    assert gate.get("layout_customized") is True
    assert gate.get("edge_waypoints", {}).get("a->b") == {"x": 10.0, "y": 20.0}


def test_hierarchy_nodes_and_edges_match_code():
    state = _screenshot_state()
    state["today_surplus"] = {
        "ok": True,
        "kwh": 12.0,
        "continuous_kw": 2.4,
        "pace_scale": 0.9,
        "capped_by": None,
    }
    state["surplus_session"] = {
        "active": True,
        "goal_kwh": 12.0,
        "delivered_kwh": 3.0,
        "devices": ["192.168.1.10"],
        "suspended_by": None,
    }
    state["control_owner"] = {"level": "plan_session", "label": "Überschuss-Session"}
    gate = build_logic_gate({}, state, live=_screenshot_live())
    nodes = _by_id(gate)
    edges = _edge_map(gate)

    assert "tages_ueberschuss" in nodes
    assert "ueberschuss_session" in nodes
    assert nodes["ueberschuss_session"]["active"] is True
    assert "3.0" in nodes["ueberschuss_session"]["detail"] or "3" in nodes["ueberschuss_session"]["detail"]
    assert nodes["schiedsrichter"]["live"].get("control_owner") == "plan_session"
    assert ("tages_ueberschuss", "ueberschuss") in edges
    assert ("ueberschuss", "ueberschuss_session") in edges
    assert ("ueberschuss_session", "schiedsrichter") in edges
    assert ("lehrneuron_soc_prio", "tages_ueberschuss") in edges
    assert ("pv_sicherheit", "ueberschuss_session") in edges
    assert ("verbrauch_anomalie_hoch", "ueberschuss_session") in edges
    assert ("nacht_sperre", "ueberschuss_session") in edges
    assert ("batterie_schutz", "ueberschuss_session") in edges
    assert "stufe1_halteband" in nodes
    assert ("max_soc", "stufe1_halteband") in edges
    assert ("stufe1_halteband", "ueberschuss_session") in edges
    assert gate.get("layout_rev") == "2026-10-15-stufe1-halteband-v1"
