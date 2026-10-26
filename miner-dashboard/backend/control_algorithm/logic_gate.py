# -*- coding: utf-8 -*-
"""
Logikgattersteuerungsalgorithmus Autopilot – Transparenz-Graph.

Baut Knoten/Kanten/Pfadfarben aus dem bestehenden Tick-State.
Ändert keine Schaltlogik.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from control_algorithm.control_priority import PRIORITY_LADDER

# Wird bei Layout-Änderungen erhöht – Dashboard erkennt neue Graph-Struktur beim Poll.
LOGIC_GATE_LAYOUT_REV = "2026-10-15-stufe1-halteband-v1"


def _pct(v: Any) -> Optional[float]:
    try:
        if v is None:
            return None
        return round(float(v), 1)
    except (TypeError, ValueError):
        return None


def _f(v: Any, digits: int = 2) -> Optional[float]:
    try:
        if v is None:
            return None
        return round(float(v), digits)
    except (TypeError, ValueError):
        return None


def _node(
    *,
    id: str,
    label: str,
    kind: str,
    cadence: str,
    route: str = "idle",
    active: bool = False,
    detail: str = "",
    live: Optional[Dict[str, Any]] = None,
    info: Optional[Dict[str, Any]] = None,
    group: Optional[str] = None,
    x: float = 0,
    y: float = 0,
) -> Dict[str, Any]:
    return {
        "id": id,
        "label": label,
        "kind": kind,
        "cadence": cadence,
        "route": route,
        "active": bool(active),
        "detail": detail,
        "live": live or {},
        "info": info or {},
        "group": group,
        "x": x,
        "y": y,
    }


def _edge(
    src: str,
    dst: str,
    *,
    label: str = "",
    color: str = "idle",
    active: bool = False,
    style: str = "solid",
) -> Dict[str, Any]:
    return {
        "from": src,
        "to": dst,
        "label": label,
        "color": color,
        "active": bool(active),
        "style": style,  # solid | dashed
    }


def _cadence_meta() -> Dict[str, Dict[str, str]]:
    return {
        "3h": {
            "label": "alle 3 Stunden ein Durchlauf",
            "stroke": "dashed",
            "color_hint": "#ffb74d",
        },
        "hourly": {
            "label": "stündlich",
            "stroke": "solid",
            "color_hint": "#64b5f6",
        },
        "tick": {
            "label": "jeder Tick",
            "stroke": "solid",
            "color_hint": "#81c784",
        },
        "15min": {
            "label": "Hysterese ca. 15 min",
            "stroke": "solid",
            "color_hint": "#ce93d8",
        },
        "daily": {
            "label": "täglich abends",
            "stroke": "dashed",
            "color_hint": "#90a4ae",
        },
    }


def _parse_iso_dt(raw: Any) -> Optional[datetime]:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        dt = raw
    else:
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if getattr(dt, "tzinfo", None) is not None:
        dt = dt.replace(tzinfo=None)
    return dt


def _fmt_remain(seconds: float) -> str:
    s = max(0, int(round(seconds)))
    if s <= 0:
        return "jetzt"
    h, rem = divmod(s, 3600)
    m, r = divmod(rem, 60)
    if h > 0:
        return f"{h}h {m:02d}m"
    return f"{m}:{r:02d}"


def _build_cadence_schedule(
    config: Dict[str, Any],
    state: Dict[str, Any],
    now: Optional[datetime] = None,
) -> Dict[str, Dict[str, Any]]:
    """Nächster Durchlauf je Takt (für Banden/Knoten im Logikgatter)."""
    import os

    now = now or datetime.now()
    tick_sec = int(os.environ.get("CONTROL_TICK_INTERVAL_SECONDS", "600") or 600)
    tick_sec = max(30, tick_sec)
    weather_sec = 3 * 3600

    last_tick = _parse_iso_dt(state.get("last_tick"))
    last_weather = _parse_iso_dt(state.get("last_weather_fetch"))

    try:
        cal_h = int(config.get("calibration_hour", 23))
    except (TypeError, ValueError):
        cal_h = 23
    try:
        cal_m = int(config.get("calibration_minute", 0))
    except (TypeError, ValueError):
        cal_m = 0
    cal_h = max(0, min(23, cal_h))
    cal_m = max(0, min(59, cal_m))

    out: Dict[str, Dict[str, Any]] = {}

    # Tick: letzter Tick + Intervall
    if last_tick is not None:
        next_tick = last_tick + timedelta(seconds=tick_sec)
    else:
        next_tick = now
    if next_tick < now:
        next_tick = now
    out["tick"] = {
        "interval_seconds": tick_sec,
        "interval_label": f"alle {tick_sec // 60} min" if tick_sec >= 60 else f"alle {tick_sec}s",
        "last_at": last_tick.isoformat(timespec="seconds") if last_tick else None,
        "next_at": next_tick.isoformat(timespec="seconds"),
        "next_in_seconds": max(0, (next_tick - now).total_seconds()),
        "next_clock": next_tick.strftime("%H:%M"),
        "next_label": _fmt_remain((next_tick - now).total_seconds()),
    }

    # Wetter / 3h
    if last_weather is not None:
        next_w = last_weather + timedelta(seconds=weather_sec)
    else:
        next_w = now
    if next_w < now:
        next_w = now
    out["3h"] = {
        "interval_seconds": weather_sec,
        "interval_label": "alle 3 Stunden",
        "last_at": last_weather.isoformat(timespec="seconds") if last_weather else None,
        "next_at": next_w.isoformat(timespec="seconds"),
        "next_in_seconds": max(0, (next_w - now).total_seconds()),
        "next_clock": next_w.strftime("%H:%M"),
        "next_label": _fmt_remain((next_w - now).total_seconds()),
    }

    # Stündlich: nächste volle Stunde
    next_hour = (now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1))
    out["hourly"] = {
        "interval_seconds": 3600,
        "interval_label": "jede volle Stunde",
        "last_at": now.replace(minute=0, second=0, microsecond=0).isoformat(timespec="seconds"),
        "next_at": next_hour.isoformat(timespec="seconds"),
        "next_in_seconds": max(0, (next_hour - now).total_seconds()),
        "next_clock": next_hour.strftime("%H:%M"),
        "next_label": _fmt_remain((next_hour - now).total_seconds()),
    }

    # 15-min Hysterese: relativ zum letzten Tick / Anomalie-Fenster
    hyst_sec = 15 * 60
    base = last_tick or now
    next_15 = base + timedelta(seconds=hyst_sec)
    if next_15 < now:
        # nächstes 15-min Raster
        slot = int(now.timestamp() // hyst_sec) * hyst_sec + hyst_sec
        next_15 = datetime.fromtimestamp(slot)
    out["15min"] = {
        "interval_seconds": hyst_sec,
        "interval_label": "Hysterese ~15 min",
        "last_at": base.isoformat(timespec="seconds"),
        "next_at": next_15.isoformat(timespec="seconds"),
        "next_in_seconds": max(0, (next_15 - now).total_seconds()),
        "next_clock": next_15.strftime("%H:%M"),
        "next_label": _fmt_remain((next_15 - now).total_seconds()),
    }

    # Täglich abends: Kalibrierungszeit
    next_day = now.replace(hour=cal_h, minute=cal_m, second=0, microsecond=0)
    if next_day <= now:
        next_day = next_day + timedelta(days=1)
    out["daily"] = {
        "interval_seconds": 24 * 3600,
        "interval_label": f"täglich {cal_h:02d}:{cal_m:02d}",
        "last_at": None,
        "next_at": next_day.isoformat(timespec="seconds"),
        "next_in_seconds": max(0, (next_day - now).total_seconds()),
        "next_clock": next_day.strftime("%H:%M"),
        "next_label": _fmt_remain((next_day - now).total_seconds()),
        "next_day": next_day.strftime("%d.%m."),
    }

    return out


def _enrich_cadence_meta(
    config: Dict[str, Any],
    state: Dict[str, Any],
    now: Optional[datetime] = None,
) -> Dict[str, Dict[str, Any]]:
    meta = {k: dict(v) for k, v in _cadence_meta().items()}
    schedule = _build_cadence_schedule(config, state, now=now)
    for key, sched in schedule.items():
        if key not in meta:
            meta[key] = {}
        meta[key].update(sched)
        clock = sched.get("next_clock") or "—"
        remain = sched.get("next_label") or "—"
        interval = sched.get("interval_label") or ""
        day = sched.get("next_day")
        if key == "daily" and day:
            meta[key]["short_next"] = f"nächster {day} {clock}"
            meta[key]["band_next"] = f"nächster {day} {clock} (in {remain})"
        else:
            meta[key]["short_next"] = f"nächster in {remain}"
            meta[key]["band_next"] = f"nächster in {remain} · ~{clock}"
        if interval:
            meta[key]["label_full"] = f"{meta[key].get('label', key)} · {meta[key]['band_next']}"
        else:
            meta[key]["label_full"] = meta[key].get("label", key)
    return meta


def _route_from_direction(direction: str) -> str:
    if direction in ("mehr_verbrauch", "on"):
        return "on"
    if direction in ("weniger_verbrauch", "off"):
        return "off"
    return "idle"


def _node_emitting(node: Optional[Dict[str, Any]]) -> bool:
    """Kästchen leuchtet nur wenn es aktiviert oder angesprochen wurde."""
    if not node:
        return False
    return bool(node.get("active")) and str(node.get("route") or "") in ("on", "off")


def _is_hub_node(node: Optional[Dict[str, Any]]) -> bool:
    if not node:
        return False
    return node.get("group") == "hub" or str(node.get("id") or "").startswith("hub_")


def _apply_edge_flow(nodes: List[Dict[str, Any]], edges: List[Dict[str, Any]]) -> None:
    """Linie hat nur dann Farbe, wenn das Quell-Kästchen selbst leuchtet.

    Grün/Rot der Linie = Grün/Rot des Quell-Gatters. Kein Rot ohne rotes
    Kästchen, keine Farbe wenn das Gatter nicht angesprochen wird.
    """
    by_id = {str(n.get("id")): n for n in nodes}
    for edge in edges:
        src = by_id.get(str(edge.get("from") or ""))
        dst = by_id.get(str(edge.get("to") or ""))
        src_route = str((src or {}).get("route") or "")
        if not _node_emitting(src) or src_route not in ("on", "off"):
            edge["active"] = False
            edge["color"] = "idle"
            continue
        if dst and not _is_hub_node(dst) and not _node_emitting(dst):
            edge["active"] = False
            edge["color"] = "idle"
            continue
        edge["active"] = True
        edge["color"] = src_route


def _planning_horizon_days(config: Dict[str, Any]) -> int:
    try:
        days = int(config.get("planning_horizon_days") or 7)
    except (TypeError, ValueError):
        days = 7
    return max(1, min(14, days))


def _cp_date_key(cp: Dict[str, Any]) -> str:
    raw = cp.get("target_datetime") or cp.get("date") or ""
    if hasattr(raw, "date"):
        try:
            return raw.date().isoformat()
        except Exception:
            return ""
    text = str(raw)
    return text[:10] if len(text) >= 10 else ""


def _horizon_day_rows(
    config: Dict[str, Any],
    proj: Dict[str, Any],
    state: Dict[str, Any],
    now: datetime,
    *,
    tomorrow_ok: bool,
) -> List[Dict[str, Any]]:
    """Zeilen für 7-Tage-Horizont (Anzeige, inkl. Produktions-/Checkpoint-Daten pro Tag)."""
    horizon = _planning_horizon_days(config)
    ctx = proj.get("weather_context") if isinstance(proj.get("weather_context"), dict) else {}
    days_ctx = list(ctx.get("days") or [])
    by_off = {int(d.get("offset") or 0): d for d in days_ctx if isinstance(d, dict)}
    checkpoints = [c for c in (proj.get("checkpoints") or []) if isinstance(c, dict)]
    week_plan = state.get("energy_week_plan") if isinstance(state.get("energy_week_plan"), dict) else {}
    plan_by_date = {
        str(d.get("day_date")): d
        for d in (week_plan.get("days") or [])
        if isinstance(d, dict) and d.get("day_date")
    }
    weekday_names = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
    today = now.date()
    rows: List[Dict[str, Any]] = []
    for offset in range(horizon):
        day_date = today + timedelta(days=offset)
        date_key = day_date.isoformat()
        dc = by_off.get(offset) or {}
        plan_day = plan_by_date.get(date_key) or {}
        totals = plan_day.get("daily_totals") if isinstance(plan_day.get("daily_totals"), dict) else {}
        mode = str(dc.get("steering_mode") or plan_day.get("steering_mode") or "normal")
        tier = str(dc.get("weather_tier") or plan_day.get("weather_tier") or "normal")
        pv = _f(dc.get("pv_kwh") if dc.get("pv_kwh") is not None else totals.get("pv_kwh"), 1)
        base_kwh = _f(totals.get("base_kwh"), 1)
        hydro_kwh = _f(totals.get("hydro_kwh"), 1)
        last_pv_hour = dc.get("last_pv_hour")
        if last_pv_hour is None:
            last_pv_hour = plan_day.get("last_pv_hour")
        day_cps = [c for c in checkpoints if _cp_date_key(c) == date_key]
        cp_fail = any(not c.get("reachable", True) for c in day_cps)
        cp = day_cps[0] if day_cps else {}
        checkpoint_soc = _f(cp.get("target_soc") or cp.get("projected_soc"), 1)
        ok = True
        if mode in ("survival_mode", "pre_storm_charge") or cp_fail:
            ok = False
        if mode == "survival_surplus_ok":
            ok = True
        if not days_ctx and offset == 1:
            ok = bool(tomorrow_ok)
        if offset == 0 and not days_ctx:
            ok = True
        wd = weekday_names[day_date.weekday()]
        if offset == 0:
            title = "Heute · Passt?"
            question = "Heute · Passt?"
        elif offset == 1:
            title = "Morgen · Wie schaut's aus?"
            question = "Morgen · Wie schaut's aus?"
        else:
            title = f"{wd} · Wie schaut's aus?"
            question = f"{wd} · Wie schaut's aus?"
        if ok:
            detail = f"ok · {tier}"
            if pv is not None:
                detail += f" · PV {pv} kWh"
        else:
            detail = f"knapp · {mode if mode != 'normal' else tier}"
            if pv is not None:
                detail += f" · PV {pv} kWh"
        slots_label = "—"
        if last_pv_hour is not None:
            try:
                slots_label = f"05–{int(last_pv_hour):02d}h"
            except (TypeError, ValueError):
                slots_label = str(last_pv_hour)
        rows.append(
            {
                "id": f"horiz_d{offset + 1}",
                "offset": offset,
                "date": date_key,
                "weekday": wd,
                "label": title,
                "question": question,
                "ok": ok,
                "steering_mode": mode,
                "weather_tier": tier,
                "pv_kwh": pv,
                "base_kwh": base_kwh,
                "hydro_kwh": hydro_kwh,
                "checkpoint_soc": checkpoint_soc,
                "checkpoint_ok": not cp_fail if day_cps else None,
                "last_pv_hour": last_pv_hour,
                "slots_label": slots_label,
                "detail": detail[:110],
            }
        )
    return rows


def _apply_layout_overrides(nodes: List[Dict[str, Any]], state: Dict[str, Any]) -> bool:
    """Überlagert gespeicherte Knotenpositionen aus dem State (Dashboard-Drag).

    Neue Knoten ohne gespeicherte Position werden neben vorhandenen
    Lernfaktoren platziert (sonst wären sie außerhalb der Zoom-Ansicht).
    """
    layout = state.get("logic_gate_layout") or {}
    # Auch ältere Rev akzeptieren: Positionen behalten, fehlende Knoten ergänzen
    positions = layout.get("positions") or {}
    if not isinstance(positions, dict) or not positions:
        return False
    by_id = {n["id"]: n for n in nodes}
    applied = False
    for nid, pos in positions.items():
        if nid not in by_id or not isinstance(pos, dict):
            continue
        try:
            if "x" in pos:
                by_id[nid]["x"] = float(pos["x"])
                applied = True
            if "y" in pos:
                by_id[nid]["y"] = float(pos["y"])
                applied = True
        except (TypeError, ValueError):
            continue

    # Neue Lern-Neuronen ohne Drag-Position neben Peers setzen
    learning = [n for n in nodes if n.get("group") == "learning"]
    placed = [n for n in learning if str(n.get("id") or "") in positions]
    missing = [n for n in learning if str(n.get("id") or "") not in positions]
    if placed and missing:
        placed_sorted = sorted(placed, key=lambda n: float(n.get("y") or 0))
        # Anker = am stärksten vom Default (x=40) weggeschobener Lernknoten
        # (Dashboard-Zoom zeigt meist die per Drag verschobene Gruppe)
        def _drag_score(n: Dict[str, Any]) -> float:
            return abs(float(n.get("x") or 40) - 40.0) + abs(
                float(n.get("y") or 0) - float(
                    {"risk_neuron": 1940, "day_night_neuron": 2005, "soc_lernen": 2240}.get(
                        str(n.get("id") or ""), 2000
                    )
                )
            )

        anchor = max(placed_sorted, key=_drag_score)
        # Falls Tag/Nacht/Risiko in derselben Cluster-Nähe wie Anker: darunter anordnen
        cluster = [
            n
            for n in placed_sorted
            if abs(float(n.get("x") or 0) - float(anchor.get("x") or 0)) < 350
        ]
        if cluster:
            anchor = max(cluster, key=lambda n: float(n.get("y") or 0))
        ax = float(anchor.get("x") or 40)
        ay = float(anchor.get("y") or 2000)
        # Bekannte Reihenfolge unter dem Anker
        prefer = [
            "lehrneuron_prio_balance",
            "lehrneuron_soc_prio",
            "speicher_band_neuron",
            "dazugelernt",
            "lernfaktor_wetter_oms",
        ]
        missing_sorted = sorted(
            missing,
            key=lambda n: (
                prefer.index(n["id"]) if n.get("id") in prefer else 99,
                str(n.get("id") or ""),
            ),
        )
        for i, n in enumerate(missing_sorted):
            n["x"] = ax
            n["y"] = ay + 95.0 * (i + 1)
            applied = True
    return applied


def _append_horizon_week_tree(
    nodes: List[Dict[str, Any]],
    *,
    horizon_rows: List[Dict[str, Any]],
    horiz_proj: Dict[str, Any],
    no_active: bool,
    hold_active: bool,
    tomorrow_ok: bool,
    tomorrow_detail: str,
    x_base: float,
    y_base: float,
    x_step: float = 290.0,
) -> None:
    """7-Tage-Baum: eigene Bande unten – 7 gleichmäßige Spalten, Plan kompakt, Ja/Nein darunter."""
    ctx = horiz_proj.get("weather_context") if isinstance(horiz_proj.get("weather_context"), dict) else {}
    bad_streak = int(ctx.get("bad_streak_days") or 0)
    streak_start = int(ctx.get("bad_streak_start_offset") or 0)
    sim_ok = bool(horiz_proj.get("all_checkpoints_ok", True))
    checkpoints = [c for c in (horiz_proj.get("checkpoints") or []) if isinstance(c, dict)]
    next_cp = checkpoints[0] if checkpoints else {}
    cp_soc = _f(next_cp.get("target_soc"), 1)
    cp_time = str(next_cp.get("time") or next_cp.get("target_time") or "—")
    week_active = bool(no_active)
    prod_step = 48.0
    prod_col_w = 132.0
    prod_top = y_base - 4 * prod_step - 24
    support_y = prod_top - 68

    streak_route = "off" if bad_streak >= 2 else ("on" if bad_streak == 0 else "idle")
    nodes.append(
        _node(
            id="horizon_sim",
            label="Batterie-Simulation",
            kind="input",
            cadence="hourly",
            route="on" if sim_ok else "off",
            active=bool(horiz_proj),
            detail=("Checkpoints ok" if sim_ok else "Checkpoint knapp")[:110],
            live={"all_checkpoints_ok": sim_ok},
            info={
                "title": "Batterie-Horizont-Simulation",
                "purpose": "SOC-Verlauf über 7 Tage inkl. PV, Verbrauch, Miner-Budget.",
                "formula": "simulate_battery_horizon",
                "sources": ["horizon_projection", "checkpoints"],
                "module": "energy_balance.simulate_battery_horizon",
                "thresholds": "Checkpoint reachable",
            },
            group="horizon",
            x=x_base + 240,
            y=support_y,
        )
    )
    nodes.append(
        _node(
            id="schlechtwetter_streak",
            label="Schlechtwetter-Streak",
            kind="input",
            cadence="hourly",
            route=streak_route,
            active=bad_streak > 0,
            detail=f"{bad_streak} Tage ab Offset {streak_start}"[:110],
            live={"bad_streak_days": bad_streak, "start_offset": streak_start},
            info={
                "title": "Schlechtwetter-Streak",
                "purpose": "Aufeinanderfolgende schlechte PV-Tage im 7-Tage-Fenster.",
                "formula": "detect_bad_streak(classify_weather_window)",
                "sources": ["weather_context.bad_streak_*"],
                "module": "weather_survival.weather_context",
                "thresholds": "≥2 Tage → Survival-Vorsorge",
            },
            group="horizon",
            x=x_base + 480,
            y=support_y,
        )
    )
    nodes.append(
        _node(
            id="dynam_soc_ziele",
            label="Dynam. SoC-Ziele",
            kind="input",
            cadence="hourly",
            route="on" if cp_soc is not None else "idle",
            active=bool(checkpoints),
            detail=f"nächstes Ziel {cp_soc}% @ {cp_time}"[:110],
            live={"target_soc": cp_soc, "target_time": cp_time},
            info={
                "title": "Dynamische SoC-Ziele",
                "purpose": "Rollierende Checkpoint-Ziele aus Horizont-Rechnung.",
                "formula": "soc_targets + simulate_battery_horizon",
                "sources": ["checkpoints", "soc_targets"],
                "module": "energy_balance.compute_horizon_projection",
                "thresholds": "proj. SOC vs. Ziel",
            },
            group="horizon",
            x=x_base + 720,
            y=support_y,
        )
    )

    prod_defs = (
        ("pv", "PV kWh/Tag", "pv_kwh", " kWh"),
        ("wetter", "Wetter-Tier", "weather_tier", ""),
        ("modus", "Steuerungsmodus", "steering_mode", ""),
        ("verbrauch", "Verbrauch kWh", "base_kwh", " kWh"),
        ("hydro", "Hydro kWh", "hydro_kwh", " kWh"),
        ("soc", "SoC-Checkpoint", "checkpoint_soc", " %"),
        ("slots", "PV-Tages-Slots", "slots_label", ""),
    )

    for i, row in enumerate(horizon_rows):
        offset = int(row.get("offset") if row.get("offset") is not None else i)
        day_num = offset + 1
        x_q = x_base + offset * x_step
        y_q = y_base
        day_ok = bool(row.get("ok"))
        if not day_ok and week_active:
            day_route, day_active = "off", True
        elif day_ok and week_active and hold_active and (i == 0 or i == len(horizon_rows) - 1):
            day_route, day_active = "on", True
        elif day_ok and week_active:
            day_route, day_active = "on", True
        else:
            day_route, day_active = "idle", False

        for j, (suffix, plabel, key, suffix_unit) in enumerate(prod_defs):
            val = row.get(key)
            if val is None:
                pdetail = "—"
            elif suffix_unit == " kWh" and isinstance(val, (int, float)):
                pdetail = f"{val:.1f}{suffix_unit}"
            elif suffix_unit == " %" and isinstance(val, (int, float)):
                pdetail = f"{val:.0f}{suffix_unit}"
            else:
                pdetail = f"{val}{suffix_unit}".strip()
            col_i = j // 4
            row_i = j % 4
            nodes.append(
                _node(
                    id=f"horiz_d{day_num}_{suffix}",
                    label=plabel,
                    kind="compact",
                    cadence="hourly",
                    route=day_route,
                    active=day_active,
                    detail=pdetail[:110],
                    live={key: val, "date": row.get("date"), "weekday": row.get("weekday")},
                    info={
                        "title": f"{row.get('weekday')} · {plabel}",
                        "purpose": f"Produktions-/Planblock Tag {day_num} ({row.get('date')}).",
                        "formula": "weather_context + energy_week_plan",
                        "sources": ["horizon_projection", "energy_week_plan"],
                        "module": "energy_balance.build_weekly_energy_plan",
                        "thresholds": str(row.get("detail") or ""),
                    },
                    group="horizon",
                    x=x_q + col_i * prod_col_w,
                    y=prod_top + row_i * prod_step,
                )
            )

        nodes.append(
            _node(
                id=str(row["id"]),
                label=str(row.get("question") or row.get("label") or f"Tag {day_num}"),
                kind="decision",
                cadence="hourly",
                route=day_route,
                active=day_active,
                detail=str(row.get("detail") or tomorrow_detail)[:110],
                live={
                    "offset": offset,
                    "date": row.get("date"),
                    "weekday": row.get("weekday"),
                    "ok": day_ok,
                    "steering_mode": row.get("steering_mode"),
                    "weather_tier": row.get("weather_tier"),
                    "pv_kwh": row.get("pv_kwh"),
                    "tomorrow_ok": tomorrow_ok,
                },
                info={
                    "title": str(row.get("question") or row.get("label")),
                    "purpose": (
                        "Tages-Entscheid im 7-Tage-Baum: Ja = Reserve hält, "
                        "Nein = knapp → Last reduzieren / weiter zum nächsten Tag."
                    ),
                    "formula": "weather_context + checkpoints",
                    "sources": ["horizon_projection.weather_context", "checkpoints"],
                    "module": "energy_balance + weather_survival",
                    "thresholds": "survival/pre_storm/Checkpoint → Nein",
                },
                group="horizon",
                x=x_q,
                y=y_q,
            )
        )

        ja_active = bool(day_ok and week_active)
        nein_active = bool(not day_ok and week_active)
        nodes.append(
            _node(
                id=f"horiz_d{day_num}_ja",
                label="Ja",
                kind="gate",
                cadence="hourly",
                route="on" if ja_active else "idle",
                active=ja_active,
                detail="Reserve/Checkpoint ok"[:110],
                live={"ok": day_ok},
                info={
                    "title": f"{row.get('weekday')} · Ja",
                    "purpose": "Tag ist tragfähig – weiter zum nächsten Wochentag.",
                    "formula": "ok=True",
                    "sources": [str(row["id"])],
                    "module": "logic_gate",
                    "thresholds": "—",
                },
                group="horizon",
                x=x_q,
                y=y_q + 62,
            )
        )
        nodes.append(
            _node(
                id=f"horiz_d{day_num}_nein",
                label="Nein",
                kind="gate",
                cadence="hourly",
                route="off" if nein_active else "idle",
                active=nein_active,
                detail="knapp / Survival"[:110],
                live={"ok": day_ok},
                info={
                    "title": f"{row.get('weekday')} · Nein",
                    "purpose": "Tag knapp – ggf. Last reduzieren, trotzdem nächster Tag prüfen.",
                    "formula": "ok=False",
                    "sources": [str(row["id"])],
                    "module": "logic_gate",
                    "thresholds": "survival_mode / Checkpoint",
                },
                group="horizon",
                x=x_q,
                y=y_q + 118,
            )
        )


def _hub(
    *,
    id: str,
    label: str,
    route: str,
    x: float,
    y: float,
    group: str = "hub",
) -> Dict[str, Any]:
    return _node(
        id=id,
        label=label,
        kind="gate",
        cadence="tick",
        route=route,
        active=route in ("on", "off"),
        detail="Sammelpunkt",
        live={},
        info={
            "title": label,
            "purpose": "Fan-in-Hub: bündelt Eingänge, damit die Linien nicht übereinanderliegen.",
            "formula": "nur Visualisierung – keine eigene Rechnung",
            "sources": ["nachgelagerte Module"],
            "module": "logic_gate hub",
            "thresholds": "—",
        },
        group=group,
        x=x,
        y=y,
    )


def build_logic_gate(
    config: Dict[str, Any],
    state: Dict[str, Any],
    live: Optional[Dict[str, Any]] = None,
    devices: Optional[List[Dict[str, Any]]] = None,
    signals: Optional[List[Dict[str, Any]]] = None,
    score: Optional[Dict[str, Any]] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Strukturierter Logikgatter-Graph (Spacing + Hubs + Linienalgorithmus)."""
    now = now or datetime.now()
    live = live or {}
    devices = devices or []
    signals = signals or []
    score = score or {}

    min_soc = float(config.get("min_soc_percent") or 35.0)
    max_soc = float(config.get("max_soc_percent") or 95.0)
    soc = live.get("soc_percent")
    if soc is None:
        soc = state.get("live_soc")
    soc_f = _pct(soc)
    pv_kw = _f(live.get("pv_kw") or state.get("live_pv_kw"), 2)
    verbrauch_kw = _f(live.get("verbrauch_kw"), 2)
    house_kw = _f(live.get("house_kw") or live.get("wohnhaus_kw"), 2)
    hydro_kw = _f(live.get("hydro_kw"), 2)
    battery_power_kw = _f(live.get("battery_power_kw"), 2)
    grid_kw = _f(live.get("grid_kw"), 2)

    ov = state.get("pv_forecast_override") or {}
    pace = _pct(ov.get("pace_deviation_pct"))
    pattern = state.get("pv_pattern_estimate") or {}
    cons_pat = state.get("consumption_pattern_estimate") or {}
    cons_ov = state.get("consumption_pattern_override") or {}
    proj = state.get("balance_projection") or state.get("horizon_projection") or {}
    prio_plan = state.get("priority_runtime_plan") or {}
    late = state.get("late_start_plan") or {}
    drain = state.get("morning_drain_plan") or {}
    fill = state.get("fill_schedule") or {}
    stretch = state.get("anchor_stretch") or {}
    curve = state.get("curve_plan") or {}
    near = proj.get("near_term_plan") or {}
    actions = state.get("planned_actions") or []
    steer_payload: Dict[str, Any] = {}
    try:
        from control_algorithm.steering_learning import learning_status_payload

        steer_payload = learning_status_payload(state) or {}
    except Exception:
        steer_payload = {}
    fl = state.get("forecast_pace_learning") or {}

    surplus_kwh = _f(
        proj.get("miner_surplus_kwh")
        if proj.get("miner_surplus_kwh") is not None
        else prio_plan.get("surplus_kwh"),
        2,
    )
    budget_kw = _f(proj.get("miner_budget_kw"), 2)
    hours_left = _f(proj.get("hours_left") or prio_plan.get("hours_left"), 2)
    binding = str(proj.get("binding_constraint") or "")
    risk = _pct(proj.get("risk_factor_percent") or config.get("risk_factor_percent"))
    proactive_kw = _f(proj.get("proactive_discharge_kw"), 2)

    export_kw = 0.0
    try:
        from control_algorithm.weather_survival import live_grid_export_kw

        export_kw = float(live_grid_export_kw(live) or 0)
    except Exception:
        export_kw = float(live.get("export_kw") or live.get("grid_export_kw") or 0)

    # Tages-Überschuss und laufender Zug (Planebene, seit Hierarchie-Umbau)
    def _num(raw: Any, digits: int = 2) -> float:
        try:
            return round(float(raw or 0.0), digits)
        except (TypeError, ValueError):
            return 0.0

    today_surplus = dict(state.get("today_surplus") or {})
    today_kwh = _num(today_surplus.get("kwh"))
    today_kw = _num(today_surplus.get("continuous_kw"))
    session = dict(state.get("surplus_session") or {})
    session_active = bool(session.get("active"))
    session_suspended_by = str(session.get("suspended_by") or "")
    session_goal_kwh = _num(session.get("goal_kwh"))
    session_done_kwh = _num(session.get("delivered_kwh"))
    session_rest_kwh = round(max(0.0, session_goal_kwh - session_done_kwh), 2)
    control_owner = dict(state.get("control_owner") or {})

    bat_lock = bool(state.get("min_soc_lock_active"))
    max_soc_prot = bool(state.get("max_soc_protection_active"))
    night = bool(state.get("night_lock_active"))
    safety = bool(state.get("pv_safety_active"))
    safety_enabled = bool(state.get("pv_safety_enabled_effective", True))
    try:
        from control_algorithm.pv_safety_watchdog import normalize_enabled

        safety_enabled = normalize_enabled(config.get("pv_safety_enabled", True))
    except Exception:
        safety_enabled = bool(config.get("pv_safety_enabled", True))
    safety_suspended = bool(state.get("pv_safety_suspended"))
    anom = bool(state.get("anomaly_active"))
    anom_low = bool(state.get("anomaly_low_active"))
    export_guard = bool(state.get("export_guard_active"))
    stretch_active = bool(stretch.get("active"))
    late_active = bool(late.get("defer") and not late.get("urgent"))
    fill_flex = fill.get("flex") or {}
    fill_now = bool(fill_flex.get("should_start_now") or fill_flex.get("planned_on"))
    fill_urgent = bool(state.get("fill_urgency_active"))
    if not fill_urgent:
        try:
            from control_algorithm.fill_schedule import compute_fill_urgency

            fill_urgent = bool(compute_fill_urgency(fill, config))
        except Exception:
            fill_urgent = bool(fill.get("over_max") or fill.get("capacity_limited"))
    morning_threat = bool(state.get("morning_floor_threatened"))
    daytime_pv_hold = bool(state.get("daytime_pv_hold"))
    algo_enabled = bool(config.get("enabled", True))
    dry_run = bool(
        state.get("dry_run")
        if state.get("dry_run") is not None
        else config.get("dry_run", True)
    )
    cons_risk_pct = _pct(config.get("consumption_risk_percent") or 0)
    cons_risk_active = cons_risk_pct is not None and cons_risk_pct > 0.05
    max_soc_over = soc_f is not None and soc_f > max_soc + 0.3
    max_soc_near = soc_f is not None and soc_f >= max_soc - 5.0
    import_kw = 0.0
    if grid_kw is not None and grid_kw > 0.05:
        import_kw = float(grid_kw)
    elif export_kw < 0.05 and grid_kw is not None and grid_kw > 0:
        import_kw = float(grid_kw)

    base_load_scale = _f(state.get("base_load_scale"), 3)
    battery_eff = _f(state.get("battery_efficiency"), 3)
    soc_learn_active = bool(
        (base_load_scale is not None and abs(base_load_scale - 1.0) >= 0.02)
        or (battery_eff is not None and abs(battery_eff - 0.95) >= 0.01)
        or state.get("last_soc_calibration")
    )
    hour_factors = state.get("hour_slot_factors") or {}
    hour_adj = [
        (k, _f(v, 3))
        for k, v in hour_factors.items()
        if _f(v, 3) is not None and abs(float(v) - 1.0) >= 0.02
    ]
    hour_slots_active = bool(hour_adj)

    run_through: Dict[str, Any] = {}
    try:
        from control_algorithm.load_scheduler import weather_run_through_floor

        sched_devs = [
            {
                "ip": d.get("ip"),
                "power_kw": d.get("power_kw"),
                "priority": d.get("priority"),
            }
            for d in devices
            if d.get("ip")
        ]
        if sched_devs:
            run_through = weather_run_through_floor(config, state, live, sched_devs) or {}
    except Exception:
        run_through = {}
    run_through_active = bool(run_through.get("active"))

    prio_violations: List[Dict[str, Any]] = []
    try:
        from control_algorithm.planner import find_priority_violations

        miner_st = {
            str(d.get("ip")): ("running" if d.get("running") else "paused")
            for d in devices
            if d.get("ip")
        }
        prio_violations = find_priority_violations(
            [
                {
                    "ip": d.get("ip"),
                    "power_kw": d.get("power_kw"),
                    "priority": d.get("priority"),
                }
                for d in devices
                if d.get("ip")
            ],
            miner_st,
        )
    except Exception:
        prio_violations = []
    prio_force_resume = any(
        a.get("force") and a.get("action") == "resume" and "Priorit" in str(a.get("reason") or "")
        for a in actions
    )
    prio_active = bool(prio_violations or prio_force_resume)

    min_off_h = _f(config.get("min_device_off_hours"), 2)
    min_off_remain = 0.0
    try:
        from control_algorithm.load_scheduler import off_stability_remaining_h

        for d in devices:
            ip = str(d.get("ip") or "")
            if not ip:
                continue
            rem = float(off_stability_remaining_h(ip, state, config) or 0)
            if rem > min_off_remain:
                min_off_remain = rem
    except Exception:
        min_off_remain = 0.0
    min_off_blocking = min_off_remain > 0.02

    plan_hold_active = False
    try:
        from control_algorithm.load_scheduler import should_hold_through_budget_dip

        near_min = bat_lock or (soc_f is not None and soc_f < min_soc + 5)
        for d in devices:
            if not d.get("running"):
                continue
            if should_hold_through_budget_dip(
                d, prio_plan, config, near_min_soc=near_min
            ) and (
                budget_kw is not None and budget_kw < float(d.get("power_kw") or 0) + 0.05
            ):
                plan_hold_active = True
                break
    except Exception:
        plan_hold_active = False

    msgs = [str(m) for m in (state.get("info_messages") or [])]
    anker_live_block = any(
        ("Anker" in m and ("nicht" in m.lower() or "block" in m.lower() or "Flex" in m))
        or "fehlende höhere Stufe" in m
        for m in msgs
    )
    anom_resume_block = False
    try:
        from control_algorithm.anomaly_detection import is_anomaly_resume_blocked

        anom_resume_block = bool(is_anomaly_resume_blocked(state, config))
    except Exception:
        anom_resume_block = False
    if anom and not anom_low:
        anom_resume_block = True

    soc_targets = [t for t in (config.get("soc_targets") or []) if isinstance(t, dict)]
    morning_soc = _pct(config.get("morning_soc_percent") or min_soc)
    cp_fail = any(
        not c.get("reachable", True)
        for c in (proj.get("checkpoints") or [])
        if isinstance(c, dict)
    )

    resumes = [a for a in actions if a.get("action") == "resume"]
    pauses = [a for a in actions if a.get("action") == "pause"]
    resume_ips = {str(a.get("ip")) for a in resumes if a.get("ip")}
    pause_ips = {str(a.get("ip")) for a in pauses if a.get("ip")}
    any_running = any(bool(d.get("running")) for d in devices)

    sig_by_id = {str(s.get("id")): s for s in signals}
    algo3 = sig_by_id.get("algo3") or {}
    global_route = _route_from_direction(str(algo3.get("direction") or ""))
    if resumes and not pauses:
        global_route = "on"
    elif pauses and not resumes:
        global_route = "off"
    elif resumes and pauses:
        global_route = "on" if len(resumes) >= len(pauses) else "off"
    if bat_lock or night or safety:
        if pauses and not resumes:
            global_route = "off"

    has_surplus = False
    if budget_kw is not None and budget_kw > 0.05:
        has_surplus = True
    if surplus_kwh is not None and surplus_kwh > 0.4:
        has_surplus = True
    if export_kw >= 0.05 or binding in (
        "export_avoidance",
        "proactive_discharge",
        "near_term_discharge",
        "max_soc",
        "survival_surplus_ok",
    ):
        has_surplus = True
    if (
        any_running
        and not pauses
        and not bat_lock
        and not night
        and not safety
        and binding
        not in ("survival_mode", "min_soc", "min_soc_emergency", "pre_storm_charge")
    ):
        has_surplus = True
    if bat_lock:
        has_surplus = False

    tomorrow_ok = True
    tomorrow_detail = "Horizont/Reserve ok – begrenzte Last möglich"
    if binding in ("survival_mode", "min_soc", "pre_storm_charge", "min_soc_emergency"):
        tomorrow_ok = False
        tomorrow_detail = f"Knapp: Bindung {binding}"
    elif bat_lock:
        tomorrow_ok = False
        tomorrow_detail = state.get("min_soc_lock_reason") or "Min-SoC verletzt"
    elif night:
        tomorrow_ok = False
        tomorrow_detail = state.get("night_lock_reason") or "Nacht-Sperre"

    anchor_h = None
    for row in prio_plan.get("devices") or []:
        if row.get("role") == "anchor":
            anchor_h = _f(row.get("planned_hours"), 2)
            break
    second_prio = bool(anchor_h is not None and anchor_h >= 24.0)

    yes_active = has_surplus and not bat_lock
    no_active = (not has_surplus) or bat_lock
    morning_shed = bool(morning_threat) and (not fill_urgent) and (not daytime_pv_hold)
    shed_active = no_active and (
        not tomorrow_ok or night or bat_lock or morning_shed
    )
    hold_active = no_active and tomorrow_ok and not bat_lock and not night
    on_path = "on" if yes_active or hold_active else "idle"
    off_path = "off" if shed_active or bat_lock or night else "idle"
    feed = global_route if global_route != "idle" else (on_path if on_path != "idle" else off_path)
    horiz_proj = dict(proj) if isinstance(proj, dict) else {}
    hp = state.get("horizon_projection") if isinstance(state.get("horizon_projection"), dict) else {}
    if not horiz_proj.get("weather_context") and hp.get("weather_context"):
        horiz_proj["weather_context"] = hp.get("weather_context")
    if not horiz_proj.get("checkpoints") and hp.get("checkpoints"):
        horiz_proj["checkpoints"] = hp.get("checkpoints")
    horizon_rows = _horizon_day_rows(config, horiz_proj, state, now, tomorrow_ok=tomorrow_ok)
    horizon_ids = [str(r["id"]) for r in horizon_rows]
    first_horiz = horizon_ids[0] if horizon_ids else "horiz_d1"

    pr = _f(state.get("performance_ratio"), 3)
    yield_factor = _f(state.get("yield_factor"), 3)
    if yield_factor is None:
        yield_factor = pr
    try:
        from control_algorithm.reference_anchor import (
            anchor_status_message,
            is_reference_anchor_active,
        )

        ref_active = bool(is_reference_anchor_active(state))
        ref_msg = anchor_status_message(state, config) or ""
    except Exception:
        ref_active = False
        ref_msg = ""

    today_sl = (state.get("steering_learning") or {}).get("today") or {}
    miss_ticks = int(today_sl.get("miss_ticks") or steer_payload.get("today_miss_ticks") or 0)
    absorb_ticks = int(today_sl.get("good_absorb_ticks") or 0)
    band_ok = int(today_sl.get("band_ok_ticks") or 0)
    adj_n = len(steer_payload.get("last_adjustments") or [])
    learn_score = _pct((steer_payload.get("params") or {}).get("score") or steer_payload.get("score"))

    angle = curve.get("consumption_angle") or {}
    # OMS und Musterschätzung: exklusiv – nur einer „greift“
    muster_engaged = bool(ov.get("active"))
    muster_w_route = "idle"
    oms_route = "idle"
    if muster_engaged:
        if pace is not None and pace <= -10:
            muster_w_route = "off"
        else:
            muster_w_route = "on"
    else:
        has_oms = (
            state.get("pv_yield_today_kwh") is not None
            or state.get("last_weather_fetch")
            or state.get("morning_pv_forecast_kwh") is not None
        )
        oms_route = "on" if has_oms else "idle"

    fl_params = fl.get("params") if isinstance(fl.get("params"), dict) else {}
    fl_today = fl.get("today") if isinstance(fl.get("today"), dict) else {}
    pace_engage = _f(fl_params.get("engage_pct"), 1)
    pace_release = _f(fl_params.get("release_pct"), 1)
    pace_trust = _f(fl_params.get("pattern_trust"), 2)
    pace_score = _pct(fl_params.get("score"))
    oms_ticks = int(fl_today.get("oms_ticks") or 0)
    override_ticks = int(fl_today.get("override_ticks") or 0)
    oms_learn_active = bool(
        fl.get("last_adjustments")
        or fl.get("last_note")
        or fl_today.get("engaged_once")
        or oms_ticks
        or override_ticks
    )

    nodes: List[Dict[str, Any]] = []
    edges: List[Dict[str, Any]] = []

    def add(
        src: str,
        dst: str,
        label: str = "",
        color: str = "idle",
        active: bool = False,
        style: str = "solid",
    ) -> None:
        edges.append(
            _edge(src, dst, label=label, color=color, active=active, style=style)
        )

    # Spalten links→rechts: Eingänge | Hubs | Kern | Plan | Entscheid | Ja | Nein | Filter | Geräte
    X0, X1, X2, XH, XC, XP, XD, XY, XN, XF, XDEV = (
        40,
        240,
        440,
        660,
        900,
        1140,
        1380,
        1620,
        2020,
        2260,
        2500,
    )
    # Wochenplaner: eigene Bande unten (getrennt vom Hauptfluss oben)
    Y_HORIZ = 1560
    HORIZ_X = 40
    HORIZ_STEP = 290
    HORIZ_PROD_TOP = Y_HORIZ - 4 * 48 - 16
    HORIZ_SUPPORT_Y = HORIZ_PROD_TOP - 72

    # ---- Weather (top left) ----
    nodes.append(
        _node(
            id="wetter_oms",
            label="Wetter OMS",
            kind="input",
            cadence="3h",
            route=oms_route,
            active=oms_route != "idle",
            detail=(
                "OMS greift"
                if oms_route != "idle"
                else (
                    "Pause – Muster greift"
                    if muster_engaged
                    else str(state.get("weather_refresh_note") or state.get("daemon_status") or "")[:110]
                )
            ),
            live={
                "pv_today_kwh": _f(state.get("pv_yield_today_kwh"), 1),
                "pv_tomorrow_kwh": _f(state.get("pv_yield_tomorrow_kwh"), 1),
                "last_fetch": state.get("last_weather_fetch"),
                "source_active": oms_route != "idle",
                "muster_engaged": muster_engaged,
            },
            info={
                "title": "Wetter OMS (Open-Meteo)",
                "purpose": (
                    "PV-Prognose heute/morgen. Greift nur wenn kein Muster-Override aktiv "
                    "(sonst Pause – exklusiv zu Muster Wetter)."
                ),
                "formula": "PV_adj = PV_roh × PR × Stundenfaktoren; Intervall 3×3600 s",
                "sources": ["state.pv_yield_*", "state.pv_forecast_detail", "pv_forecast_override"],
                "module": "service.refresh_weather → oms_pv_forecast",
                "thresholds": "Retry bei forecast_error/fallback",
            },
            group="weather_3h",
            x=X0,
            y=300,
        )
    )
    nodes.append(
        _node(
            id="muster_wetter",
            label="Muster Wetter",
            kind="input",
            cadence="3h",
            route=muster_w_route,
            active=muster_w_route != "idle",
            detail=(
                (
                    f"Muster greift · Pace {pace:+.1f} %"
                    if pace is not None
                    else "Muster greift"
                )
                if muster_engaged
                else "Pause – OMS greift"
            ),
            live={
                "pace_deviation_pct": pace,
                "effective_kwh": ov.get("effective_kwh") or pattern.get("estimated_kwh"),
                "override_active": muster_engaged,
                "oms_kwh": ov.get("oms_kwh"),
                "reason": ov.get("reason"),
            },
            info={
                "title": "PV-Muster / Forecast-Pace",
                "purpose": (
                    "Ist-PV vs. Prognose. Greift nur bei Override (|Pace| ≥ Engage), "
                    "sonst Pause – exklusiv zu Wetter OMS."
                ),
                "formula": "pace% = (Ist−Soll)/Soll×100; Hysterese Engage/Release",
                "sources": ["state.pv_forecast_override", "state.pv_pattern_estimate"],
                "module": "pv_pattern_estimate",
                "thresholds": f"|pace| ≥ {pace_engage if pace_engage is not None else 'Engage'} %",
            },
            group="weather_3h",
            x=X0,
            y=410,
        )
    )
    nodes.append(
        _node(
            id="pv_sicherheit",
            label="PV-Sicherheit",
            kind="guard",
            cadence="tick",
            # Stein bleibt immer sichtbar; bei ausgeschaltetem Watchdog nur inaktiv.
            route="off" if (safety and safety_enabled and not safety_suspended) else "idle",
            active=bool(safety and safety_enabled and not safety_suspended),
            detail=(
                "deaktiviert – wartet auf Handschalter"
                if not safety_enabled
                else (
                    f"ruht ({state.get('pv_safety_suspended_reason') or 'Vorrang'})"
                    if safety_suspended
                    else (
                        f"bremst Session · Pace {state.get('pv_safety_deviation_pct')} %"
                        if safety
                        else "Watchdog ruhig"
                    )
                )
            )[:110],
            live={
                "pv_safety_enabled": safety_enabled,
                "pv_safety_active": safety,
                "pv_safety_suspended": safety_suspended,
                "deviation_pct": _pct(state.get("pv_safety_deviation_pct")),
                "forecast_ref": (state.get("pv_safety_signals") or {}).get("forecast_ref"),
            },
            info={
                "title": "PV-Sicherheits-Watchdog",
                "purpose": (
                    "Risiko-Bremse: pausiert den laufenden Überschuss-Zug, "
                    "fährt aber keinen eigenen Plan. Bei Max-SoC/Plan-Band ruht "
                    "die Sperre, statt gelöscht zu werden."
                ),
                "formula": "Pace gegen Live-OMS; Trigger −20 %, Clear −12 %",
                "sources": ["state.pv_safety_*", "config.pv_safety_enabled"],
                "module": "pv_safety_watchdog",
                "thresholds": "Stufe: hard_stop · schlägt Plan-Session",
            },
            group="weather_3h",
            x=X1,
            y=410,
        )
    )
    sun_factor = _f(
        (state.get("pv_forecast_detail") or {}).get("sun_factor")
        or (state.get("weather_context") or {}).get("sun_factor"),
        2,
    )
    nodes.append(
        _node(
            id="wetter_massstab",
            label="Wetter-Maßstab",
            kind="gate",
            cadence="3h",
            route=oms_route if oms_route != "idle" else "idle",
            active=oms_route != "idle" or sun_factor is not None,
            detail=(
                f"Sonne {sun_factor}" if sun_factor is not None else "WMO / Bewölkung → PV"
            ),
            live={"sun_factor": sun_factor},
            info={
                "title": "Wetter-Maßstab (fest)",
                "purpose": (
                    "WMO-Code / Bewölkung / Strahlung → relative Sonnenstunden. "
                    "Wird nicht durch Abend-Kalibrierung verändert."
                ),
                "formula": "shortwave/800 oder WMO_SUN_FACTOR × Bewölkung",
                "sources": ["weather_scale", "Open-Meteo hourly"],
                "module": "weather_scale",
                "thresholds": "0…1 (0=keine Sonne)",
            },
            group="weather_3h",
            x=X0,
            y=520,
        )
    )
    hf_txt = (
        ", ".join(f"{k}={v}" for k, v in hour_adj[:3])
        if hour_adj
        else "Stundenfaktoren = 1.0"
    )
    nodes.append(
        _node(
            id="stundenfaktoren",
            label="Stundenfaktoren",
            kind="gate",
            cadence="daily",
            route="on" if hour_slots_active else "idle",
            active=bool(hour_factors),
            detail=hf_txt[:110],
            live={
                "hour_slot_factors": hour_factors,
                "adjusted": hour_adj[:8],
            },
            info={
                "title": "PV-Stundenfaktoren",
                "purpose": (
                    "Kalibrierte h05–h22 Faktoren skalieren die OMS-Prognose. "
                    "Ändert Budget/Surplus ohne den Wetter-Maßstab anzufassen."
                ),
                "formula": "PV_adj[h] = PV_roh[h] × PR × hour_slot_factors[h]",
                "sources": ["state.hour_slot_factors"],
                "module": "slot_profiles + calibration",
                "thresholds": "|f−1| ≥ 0.02 → gelernt",
            },
            group="weather_3h",
            x=X1,
            y=520,
        )
    )

    # ---- Live (ganz oben, damit Wetter→Hub-Linien nicht durch Live laufen) ----
    nodes.append(
        _node(
            id="live_soc",
            label="Live SoC",
            kind="input",
            cadence="tick",
            route="off" if bat_lock else "idle",
            active=soc_f is not None,
            detail=f"{soc_f} %" if soc_f is not None else "keine Messung",
            live={"soc_percent": soc_f, "min": min_soc, "max": max_soc},
            info={
                "title": "Live Speicherstand",
                "purpose": "Aktueller SoC – Basis für Guards und Bilanz.",
                "formula": "SoC % vom Batteriesystem",
                "sources": ["live.soc_percent"],
                "module": "live_data",
                "thresholds": f"Min {min_soc:.0f} % · Max {max_soc:.0f} %",
            },
            group="live",
            x=X1,
            y=20,
        )
    )
    nodes.append(
        _node(
            id="live_pv",
            label="Live PV",
            kind="input",
            cadence="tick",
            route="idle",
            active=pv_kw is not None,
            detail=f"{pv_kw} kW" if pv_kw is not None else "—",
            live={"pv_kw": pv_kw, "pv_kwh_today": _f(state.get("live_pv_kwh_actual"), 1)},
            info={
                "title": "Live PV-Leistung",
                "purpose": "Momentane Erzeugung und Tages-Ist.",
                "formula": "P_pv [kW]",
                "sources": ["live.pv_kw"],
                "module": "live_data",
                "thresholds": "—",
            },
            group="live",
            x=X1,
            y=120,
        )
    )
    netz_route = "on" if export_kw >= 0.05 else ("off" if import_kw >= 0.05 else "idle")
    nodes.append(
        _node(
            id="netz_export",
            label="Netz Imp/Exp",
            kind="input",
            cadence="tick",
            route=netz_route,
            active=netz_route != "idle",
            detail=(
                f"Export {export_kw:.2f} kW"
                if export_kw >= 0.05
                else (
                    f"Bezug {import_kw:.2f} kW"
                    if import_kw >= 0.05
                    else f"Export {export_kw:.2f} kW"
                )
            ),
            live={
                "export_kw": round(export_kw, 2),
                "import_kw": round(import_kw, 2),
                "grid_kw": grid_kw,
                "verbrauch_kw": verbrauch_kw,
            },
            info={
                "title": "Netz Import / Export",
                "purpose": "Einspeisung → mehr Last; Bezug → weniger Last / knapper.",
                "formula": "grid_kw >0 Bezug, <0 Export; export_kW = max(0, −grid)",
                "sources": ["live.grid_kw", "live_grid_export_kw"],
                "module": "live_data + weather_survival",
                "thresholds": "≥ 0.05 kW",
            },
            group="live",
            x=X1,
            y=220,
        )
    )
    bat_route = "idle"
    if battery_power_kw is not None:
        if battery_power_kw <= -0.2:
            bat_route = "on"
        elif battery_power_kw >= 0.2:
            bat_route = "off"
    nodes.append(
        _node(
            id="live_verbrauch",
            label="Live Verbrauch",
            kind="input",
            cadence="tick",
            route="off" if anom else ("on" if anom_low else "idle"),
            active=verbrauch_kw is not None or house_kw is not None,
            detail=(
                f"Haus {house_kw if house_kw is not None else '—'} kW · "
                f"Gesamt {verbrauch_kw if verbrauch_kw is not None else '—'} kW"
            ),
            live={"house_kw": house_kw, "verbrauch_kw": verbrauch_kw},
            info={
                "title": "Live Verbrauch (Haus + Gesamt)",
                "purpose": (
                    "Unkontrollierte Hauslast und Gesamtverbrauch. "
                    "Geht in Surplus, Morgen-Floor-Cap und Wetter-Durchlauf ein."
                ),
                "formula": "surplus ≈ max(0, PV − Verbrauch); Haus ohne Miner",
                "sources": ["live.house_kw", "live.verbrauch_kw"],
                "module": "live_data",
                "thresholds": "Anomalie übersteuert",
            },
            group="live",
            x=X0,
            y=20,
        )
    )
    nodes.append(
        _node(
            id="live_hydro",
            label="Live Hydro",
            kind="input",
            cadence="tick",
            route="on" if (hydro_kw is not None and hydro_kw >= 0.05) else "idle",
            active=hydro_kw is not None and hydro_kw >= 0.05,
            detail=f"{hydro_kw} kW" if hydro_kw is not None else "—",
            live={"hydro_kw": hydro_kw},
            info={
                "title": "Live Wasserkraft",
                "purpose": "Zweite Erzeugung neben PV – Surplus/Export auch nachts.",
                "formula": "P_hydro [kW]",
                "sources": ["live.hydro_kw"],
                "module": "live_data",
                "thresholds": "≥ 0.05 kW → Erzeugung aktiv",
            },
            group="live",
            x=X0,
            y=120,
        )
    )
    nodes.append(
        _node(
            id="live_batterie",
            label="Live Batterie P",
            kind="input",
            cadence="tick",
            route=bat_route,
            active=bat_route != "idle",
            detail=(
                f"{battery_power_kw:+.2f} kW "
                + ("Laden" if (battery_power_kw or 0) > 0 else "Entladen")
                if battery_power_kw is not None
                else "—"
            ),
            live={"battery_power_kw": battery_power_kw, "soc_percent": soc_f},
            info={
                "title": "Live Batterie-Leistung",
                "purpose": (
                    "Laden (+) konkurriert mit Minern um PV; Entladen (−) speist Last. "
                    "Füllplan und Soll-Kurve nutzen den Wert."
                ),
                "formula": "+Laden / −Entladen [kW]",
                "sources": ["live.battery_power_kw"],
                "module": "live_data + fill_schedule + curve_plan",
                "thresholds": "|P| ≥ 0.2 kW",
            },
            group="live",
            x=X0,
            y=220,
        )
    )

    # ---- Consumption (deutlich unter Wetter+Live) ----
    nodes.append(
        _node(
            id="verbrauch_hist",
            label="Verbrauch Hist.",
            kind="input",
            cadence="daily",
            route="idle",
            active=True,
            detail="Wochentags-Profile aus Archiv",
            live={"history_days": config.get("consumption_history_days")},
            info={
                "title": "Verbrauchshistorie",
                "purpose": "Archivierte Profile (Haus ohne Miner).",
                "formula": "Grundverbrauch = max(0, Betrieb−Miner)",
                "sources": ["consumption_history"],
                "module": "consumption_history",
                "thresholds": "~7 Tage für Anomalie",
            },
            group="consumption",
            x=X0,
            y=720,
        )
    )
    nodes.append(
        _node(
            id="verbrauch_muster",
            label="Verbrauch Muster",
            kind="input",
            cadence="tick",
            route="idle",
            active=bool(cons_ov.get("active") or cons_pat.get("estimated_kwh") is not None),
            detail=str((cons_ov.get("note") or cons_pat.get("summary") or "Muster"))[:100],
            live={
                "override_active": bool(cons_ov.get("active")),
                "estimated_kwh": cons_pat.get("estimated_kwh"),
            },
            info={
                "title": "Verbrauchsmuster",
                "purpose": "Tagesverbrauch aus Mustern skalieren.",
                "formula": f"Engage ab {config.get('consumption_pattern_engage_pct', 15)} %",
                "sources": ["state.consumption_pattern_*"],
                "module": "consumption_pattern_estimate",
                "thresholds": "engage_pct",
            },
            group="consumption",
            x=X1,
            y=720,
        )
    )
    nodes.append(
        _node(
            id="verbrauch_anomalie_hoch",
            label="Anomalie Überverbrauch",
            kind="input",
            cadence="15min",
            route="off" if anom else "idle",
            active=anom,
            detail=f"+{state.get('anomaly_excess_pct')} %" if anom else "wie geplant",
            live={"anomaly_active": anom, "excess_pct": _pct(state.get("anomaly_excess_pct"))},
            info={
                "title": "Überverbrauchs-Anomalie",
                "purpose": "Ist klar über Historie → Last runter.",
                "formula": "excess% = (Ist−Hist)/Hist×100",
                "sources": ["state.anomaly_*"],
                "module": "anomaly_detection",
                "thresholds": f"{config.get('anomaly_threshold_percent', 20)} %",
            },
            group="consumption",
            x=X0,
            y=880,
        )
    )
    nodes.append(
        _node(
            id="performance_ratio",
            label="Performance Ratio",
            kind="gate",
            cadence="daily",
            route="idle",
            active=pr is not None,
            detail=f"PR {pr}" if pr is not None else "—",
            live={"performance_ratio": pr},
            info={
                "title": "Performance Ratio",
                "purpose": "Kalibrierfaktor Prognose × PR.",
                "formula": "PV_adj = PV_roh × PR",
                "sources": ["state.performance_ratio"],
                "module": "calibration",
                "thresholds": "Abend-Kalibrierung",
            },
            group="consumption",
            x=X1,
            y=880,
        )
    )
    nodes.append(
        _node(
            id="verbrauch_anomalie_tief",
            label="Anomalie Unterverbrauch",
            kind="input",
            cadence="15min",
            route="on" if anom_low else "idle",
            active=anom_low,
            detail=f"−{state.get('anomaly_deficit_pct')} %" if anom_low else "wie geplant",
            live={
                "anomaly_low_active": anom_low,
                "deficit_pct": _pct(state.get("anomaly_deficit_pct")),
            },
            info={
                "title": "Unterverbrauchs-Anomalie",
                "purpose": "Zu wenig Verbrauch → mehr Last freigeben.",
                "formula": "deficit% unter Historiemittel",
                "sources": ["state.anomaly_low_*"],
                "module": "anomaly_detection",
                "thresholds": f"{config.get('anomaly_low_threshold_percent', 20)} %",
            },
            group="consumption",
            x=X0,
            y=1040,
        )
    )
    nodes.append(
        _node(
            id="referenz_anker",
            label="Referenz-Anker",
            kind="gate",
            cadence="daily",
            route="on" if ref_active else "idle",
            active=ref_active,
            detail=(ref_msg or "Vollsonne-Referenz")[:100],
            live={"anchor_active": ref_active},
            info={
                "title": "Referenz-Anker",
                "purpose": "Vollsonne-Tagesertrag als Maßstab.",
                "formula": "ref ≈ kWp × sun_hours",
                "sources": ["reference_anchor"],
                "module": "reference_anchor",
                "thresholds": "—",
            },
            group="consumption",
            x=X1,
            y=1040,
        )
    )
    nodes.append(
        _node(
            id="abend_kalibrierung",
            label="Abend-Kalibrierung",
            kind="gate",
            cadence="daily",
            route="idle",
            active=bool(state.get("last_calibration_run_key")),
            detail=str(state.get("last_calibration_run_key") or "noch nicht heute")[:100],
            live={"last_key": state.get("last_calibration_run_key")},
            info={
                "title": "Abend-Kalibrierung",
                "purpose": "1× abends: PR, Steering-Learning, Pace-Lernen.",
                "formula": "Fenster ab calibration_hour:minute bis Mitternacht (1×/Tag)",
                "sources": ["state.last_calibration_run_key"],
                "module": "service.maybe_run_calibration",
                "thresholds": "1×/Tag",
            },
            group="consumption",
            x=X1,
            y=1200,
        )
    )
    nodes.append(
        _node(
            id="soc_lernen",
            label="SoC-Lernen",
            kind="gate",
            cadence="daily",
            route="off" if soc_learn_active else "idle",
            active=soc_learn_active,
            detail=(
                f"Grundlast ×{base_load_scale if base_load_scale is not None else '1'} · "
                f"η {battery_eff if battery_eff is not None else '—'}"
            )[:110],
            live={
                "base_load_scale": base_load_scale,
                "battery_efficiency": battery_eff,
                "soc_accuracy_score": _pct(state.get("soc_accuracy_score")),
            },
            info={
                "title": "SoC-Lernen",
                "purpose": (
                    "Morgenprognose vs. Ist-SoC → Grundlast-Skala und Akku-Wirkungsgrad. "
                    "Ändert die Horizont-Simulation und damit Budget/EIN-AUS."
                ),
                "formula": "Verbrauch_sim × base_load_scale; Speichern × η",
                "sources": ["state.base_load_scale", "state.battery_efficiency"],
                "module": "soc_calibration",
                "thresholds": "|scale−1|≥0.02 oder |η−0.95|≥0.01",
            },
            group="learning",
            x=X0,
            y=2240,
        )
    )

    weather_hub_route = (
        "off" if safety else (muster_w_route if muster_engaged else oms_route)
    )
    if bat_lock or import_kw >= 0.05 or bat_route == "off":
        live_hub_route = "off"
    elif export_kw >= 0.05 or bat_route == "on" or (
        hydro_kw is not None and hydro_kw >= 0.05
    ):
        live_hub_route = "on"
    else:
        live_hub_route = "idle"
    verbrauch_hub_route = (
        "off"
        if anom or cons_risk_active
        else ("on" if anom_low or ref_active or soc_learn_active else "idle")
    )

    # ---- Fan-in hubs (Live oben, Wetter mitte, Verbrauch unten) ----
    nodes.append(
        _hub(id="hub_live", label="Hub Live", route=live_hub_route, x=XH, y=100, group="hub")
    )
    nodes.append(
        _hub(
            id="hub_wetter",
            label="Hub Wetter",
            route=weather_hub_route,
            x=XH,
            y=340,
            group="hub",
        )
    )
    nodes.append(
        _hub(
            id="hub_verbrauch",
            label="Hub Verbrauch",
            route=verbrauch_hub_route,
            x=XH,
            y=880,
            group="hub",
        )
    )

    # ---- Core ----
    nodes.append(
        _node(
            id="energiebilanz",
            label="Energiebilanz",
            kind="gate",
            cadence="hourly",
            route=feed,
            active=True,
            detail=(
                f"Budget {budget_kw} kW · Surplus {surplus_kwh} kWh"
                if budget_kw is not None or surplus_kwh is not None
                else "Horizont-Simulation"
            ),
            live={
                "miner_budget_kw": budget_kw,
                "miner_surplus_kwh": surplus_kwh,
                "hours_left": hours_left,
                "binding": binding or None,
            },
            info={
                "title": "Energiebilanz",
                "purpose": "Horizont-Simulation → Surplus & Budget.",
                "formula": "surplus≈max_kW×h×risk; budget≈surplus/h",
                "sources": ["state.balance_projection"],
                "module": "energy_balance",
                "thresholds": "stündlich",
            },
            group="core",
            x=XC,
            y=80,
        )
    )
    nodes.append(
        _node(
            id="risk_faktor",
            label="Risk-Faktor",
            kind="gate",
            cadence="hourly",
            route="off" if (risk is not None and risk < 99.5) else "idle",
            active=risk is not None and risk < 99.5,
            detail=f"{risk:.0f} %" if risk is not None else "—",
            live={"risk_factor_percent": risk},
            info={
                "title": "Risk-Faktor",
                "purpose": "Nur X % des Surplus gelten als sicher.",
                "formula": "surplus = raw × (risk%/100)",
                "sources": ["config.risk_factor_percent"],
                "module": "energy_balance",
                "thresholds": "<100 % → Reserve",
            },
            group="core",
            x=XC,
            y=190,
        )
    )
    nodes.append(
        _node(
            id="verbrauch_risiko",
            label="Verbrauch-Risiko",
            kind="gate",
            cadence="hourly",
            route="off" if cons_risk_active else "idle",
            active=cons_risk_active,
            detail=(
                f"+{cons_risk_pct:.0f} % auf Hist.-Verbrauch"
                if cons_risk_pct is not None
                else "kein Aufschlag"
            ),
            live={"consumption_risk_percent": cons_risk_pct},
            info={
                "title": "Verbrauchsrisiko",
                "purpose": (
                    "Aufschlag auf historischen Hausverbrauch in der Energiebilanz. "
                    "Höherer Plan-Verbrauch → weniger Miner-Surplus."
                ),
                "formula": "Verbrauch_plan = Hist × (1 + risk%/100) × Muster-Scale",
                "sources": ["config.consumption_risk_percent"],
                "module": "energy_balance.consumption_risk_scale",
                "thresholds": ">0 % → knapper rechnen",
            },
            group="core",
            x=XC,
            y=300,
        )
    )
    tgt_txt = ", ".join(
        f"{t.get('time')} {t.get('soc_percent')}%"
        for t in soc_targets[:3]
    ) or f"Morgen-Floor {morning_soc}%"
    nodes.append(
        _node(
            id="soc_ziele",
            label="SoC-Ziele / Checkpoints",
            kind="gate",
            cadence="hourly",
            route="off" if cp_fail or morning_threat else "idle",
            active=bool(soc_targets) or morning_soc is not None,
            detail=tgt_txt[:110],
            live={
                "morning_soc_percent": morning_soc,
                "soc_targets": soc_targets[:6],
                "checkpoint_fail": cp_fail,
            },
            info={
                "title": "SoC-Ziele und Checkpoints",
                "purpose": (
                    "Morgen-Floor und Uhrzeit-Ziele der Horizont-Simulation. "
                    "Verfehlt → Survival / Last reduzieren."
                ),
                "formula": "required_soc_at_datetime + morning_soc_percent",
                "sources": ["config.soc_targets", "config.morning_soc_percent", "checkpoints"],
                "module": "planner + weather_survival + energy_balance",
                "thresholds": "Checkpoint nicht reachable → knapp",
            },
            group="core",
            x=XC,
            y=410,
        )
    )

    # Linienalgorithmus (curve_plan)
    steer = str(curve.get("steer") or "")
    linien_route = feed
    if steer == "mehr_last_frueher":
        linien_route = "on"
    elif steer == "weniger_last_oder_warten":
        linien_route = "off"
    nodes.append(
        _node(
            id="linienalgorithmus",
            label="Linienalgorithmus",
            kind="gate",
            cadence="tick",
            route=linien_route if curve.get("ok") else "idle",
            active=bool(curve.get("ok")),
            detail=str(curve.get("formula") or "Soll-Kurve SoC + Verbrauchswinkel")[:110],
            live={
                "steer": steer or None,
                "desired_charge_kw": curve.get("desired_charge_kw"),
                "ist_charge_kw": curve.get("ist_charge_kw"),
                "slope_error_kw": curve.get("slope_error_kw"),
                "peak_hour": curve.get("peak_hour"),
                "phase": curve.get("phase"),
                "angle_shape": angle.get("shape"),
                "angle_ratio": angle.get("angle_ratio"),
                "soll_deg": (curve.get("display") or {}).get("angles", {}).get("soll_deg"),
                "ist_deg": (curve.get("display") or {}).get("angles", {}).get("ist_deg"),
                "delta_deg": (curve.get("display") or {}).get("angles", {}).get("delta_deg"),
                "line_action": (curve.get("display") or {}).get("line_action"),
                "points": curve.get("points") or [],
            },
            info={
                "title": "Linienalgorithmus (Soll-Kurve)",
                "purpose": (
                    "Blaue Linie = SoC-Soll bis Max. Rote Linie = SoC-Ist bei aktueller Ladung. "
                    "Orange Max-Linie = Einschaltung, rote Min-Linie = Notabschaltung. "
                    "Wenn der Ladewinkel um ≥6° vom Soll abweicht → Lastkorrektur. "
                    "Wird in run_tick vor maybe_run_hourly_balance berechnet (Live-Eingang)."
                ),
                "formula": str(
                    curve.get("formula")
                    or "desired_charge_kw flach bis peak_hour; slope_error = Ist−Soll; ΔWinkel ≥ 6°"
                ),
                "sources": [
                    "state.curve_plan.points (blau Soll)",
                    "state.curve_plan.ist_points (rot Ist-Projektion)",
                    "config.min_soc_percent / max_soc_percent (Schwellwerte)",
                    "desired_charge_kw / ist_charge_kw / Winkel °",
                ],
                "module": "curve_plan.compute_target_soc_curve",
                "thresholds": (
                    "SoC < Min → Notabschaltung; SoC > Max → Einschaltung; "
                    "|ΔWinkel| ≥ 6° oder |slope_error| > 0.4 kW → Lastkorrektur"
                ),
            },
            group="core",
            x=XP,
            y=80,
        )
    )
    nodes.append(
        _node(
            id="autopilot_survival",
            label="Autopilot / Survival",
            kind="gate",
            cadence="hourly",
            route="off" if not tomorrow_ok else "idle",
            active=not tomorrow_ok,
            detail=tomorrow_detail[:110],
            live={
                "tomorrow_ok": tomorrow_ok,
                "binding": binding or None,
                "pv_tomorrow_kwh": _f(state.get("pv_yield_tomorrow_kwh"), 1),
            },
            info={
                "title": "Autopilot / Survival",
                "purpose": "Morgen-Checkpoints & Schlechtwetter-Reserve.",
                "formula": "SoC-Simulation über Nacht/morgen",
                "sources": ["weather_survival", "binding"],
                "module": "weather_survival + energy_balance",
                "thresholds": "survival_mode → drosseln",
            },
            group="core",
            x=XP,
            y=190,
        )
    )
    nodes.append(
        _node(
            id="steering_learning",
            label="Steering-Learning",
            kind="gate",
            cadence="tick",
            route="idle",
            active=bool(state.get("steering_learning")),
            detail=f"Score {learn_score}/100" if learn_score is not None else "Beobachtung",
            live={
                "score": learn_score,
                "temper": state.get("steering_temper") or state.get("learning_temper_note"),
            },
            info={
                "title": "Steering-Learning",
                "purpose": "Lernt aus Ticks; temperiert aggressive Last.",
                "formula": "record_tick_observation + Abend-Anpassung",
                "sources": ["steering_learning"],
                "module": "steering_learning",
                "thresholds": "Abend-Run",
            },
            group="learning",
            x=X0,
            y=1880,
        )
    )
    # Risiko-Neuron: Lernabteil (wie Wetter-OMS), 2 Anschlüsse → Schalt-Filter
    risk_params = {}
    try:
        from control_algorithm.steering_learning import get_steering_params

        sp = get_steering_params(state)
        risk_params = {
            "extra_runtime_h": sp.get("switch_risk_extra_runtime_h"),
            "extra_off_h": sp.get("switch_risk_extra_off_h"),
            "session_bias": sp.get("switch_risk_session_bias"),
        }
    except Exception:
        pass
    sl_today = ((state.get("steering_learning") or {}).get("today") or {})
    risk_active = (
        float(risk_params.get("extra_runtime_h") or 0) > 0.05
        or float(risk_params.get("extra_off_h") or 0) > 0.05
        or float(risk_params.get("session_bias") or 0) > 0.05
        or int(sl_today.get("on_limit_blocks") or 0) > 0
    )
    nodes.append(
        _node(
            id="risk_neuron",
            label="Risiko-Neuron Schaltbudget",
            kind="gate",
            cadence="tick",
            route="on" if risk_active else "idle",
            active=risk_active or bool(state.get("steering_learning")),
            detail=(
                f"Blocks {sl_today.get('on_limit_blocks') or 0} · "
                f"An+{float(risk_params.get('extra_runtime_h') or 0):.1f}h · "
                f"Aus+{float(risk_params.get('extra_off_h') or 0):.1f}h · "
                f"Bias {float(risk_params.get('session_bias') or 0):.2f}"
            )[:110],
            live={
                "on_limit_blocks": sl_today.get("on_limit_blocks"),
                "on_limit_miss_ticks": sl_today.get("on_limit_miss_ticks"),
                "last_risk_note": (state.get("steering_learning") or {}).get(
                    "last_risk_note"
                ),
                **risk_params,
            },
            info={
                "title": "Risiko-Neuron (Schaltbudget)",
                "purpose": (
                    "Lernfaktor im Abteil LERNFAKTOREN: Wenn EIN-Limit greift und "
                    "Ziel verfehlt wird (Export/Max-SoC), lernt längere Sessions "
                    "und speist 2× in die Schalt-Filter (Limit + Commitment)."
                ),
                "formula": "record_on_limit_block + Abendkalibrierung → switch_risk_*",
                "sources": ["steering_learning.switch_risk_*", "switch_risk"],
                "module": "steering_learning",
                "thresholds": "Block+Export → Sofort-EMA; Abend bei ≥3 Blocks+Miss",
            },
            group="learning",
            x=X0,
            y=1940,
        )
    )
    try:
        from control_algorithm.steering_learning import (
            day_night_shift_bias,
            night_absorption_max_fraction,
        )

        dn_bias = day_night_shift_bias(state)
        dn_frac = night_absorption_max_fraction(state)
    except Exception:
        dn_bias = 0.0
        dn_frac = 0.15
    absorb = state.get("absorption_plan_24h") or {}
    dn_active = (
        float(absorb.get("total_need_kwh") or 0) > 1.0
        or abs(dn_bias) > 0.1
        or dn_frac != 0.15
        or int(sl_today.get("night_switch_count") or 0) > 0
    )
    nodes.append(
        _node(
            id="day_night_neuron",
            label="Tag/Nacht-Neuron",
            kind="gate",
            cadence="3h",
            route="on" if dn_active else "idle",
            active=dn_active,
            detail=(
                f"Nacht max {dn_frac * 100:.0f}% · Bias {dn_bias:.2f} · "
                f"Absorb {float(absorb.get('total_need_kwh') or 0):.0f} kWh"
            )[:110],
            live={
                "day_night_shift_bias": dn_bias,
                "night_absorption_max_fraction": dn_frac,
                "absorption_plan_24h": absorb,
                "night_switch_count": sl_today.get("night_switch_count"),
                "morning_export_kwh": sl_today.get("morning_export_kwh"),
            },
            info={
                "title": "Tag/Nacht-Neuron (Absorptions-Timing)",
                "purpose": (
                    "Steuert wie viel des 24h-Absorptions-Budgets nachts vs. tagsüber "
                    "eingeplant wird. Lernt aus Morgen-Export, nutzlosen Nacht-EIN "
                    "und Min-SoC tagsüber."
                ),
                "formula": "compute_24h_absorption_plan + day_night_shift_bias",
                "sources": [
                    "absorption_plan_24h",
                    "steering_learning.day_night_*",
                ],
                "module": "steering_learning + energy_balance",
                "thresholds": "night_fraction 0–40%",
            },
            group="learning",
            x=X0,
            y=2005,
        )
    )
    try:
        from control_algorithm.peak_balance_learning import (
            anchor_conserve_bias,
            peak_export_risk,
            peak_spill_boost,
        )

        lehr_spill = peak_spill_boost(state)
        lehr_anchor = anchor_conserve_bias(state)
        lehr_risk = peak_export_risk(config, state, live)
    except Exception:
        lehr_spill = 0.0
        lehr_anchor = 0.0
        lehr_risk = False
    lehr_lit = (
        lehr_spill > 0.05
        or lehr_anchor > 0.05
        or lehr_risk
        or int(sl_today.get("peak_pv_ticks") or 0) > 0
        or bool((state.get("steering_learning") or {}).get("last_lehr_note"))
    )
    # Immer sichtbar im Lernband (wie Risiko-Neuron), auch bevor erste Ticks da sind
    lehr_active = True
    nodes.append(
        _node(
            id="lehrneuron_prio_balance",
            label="Lehrneuron Prio-Balance",
            kind="gate",
            cadence="tick",
            route="on" if lehr_lit else "idle",
            active=lehr_active,
            detail=(
                f"Spill {lehr_spill:.2f} · Anker {lehr_anchor:.2f} · "
                f"Peak {int(sl_today.get('peak_pv_ticks') or 0)} Ticks · "
                f"Miss {int(sl_today.get('peak_export_miss_ticks') or 0)}"
            )[:110],
            live={
                "peak_spill_boost": lehr_spill,
                "anchor_conserve_bias": lehr_anchor,
                "peak_export_risk": lehr_risk,
                "peak_pv_ticks": sl_today.get("peak_pv_ticks"),
                "peak_export_miss_ticks": sl_today.get("peak_export_miss_ticks"),
                "peak_overparallel_ticks": sl_today.get("peak_overparallel_ticks"),
                "peak_good_absorb_ticks": sl_today.get("peak_good_absorb_ticks"),
                "last_lehr_note": (state.get("steering_learning") or {}).get(
                    "last_lehr_note"
                ),
            },
            info={
                "title": "Lehrneuron Prio-Balance",
                "purpose": (
                    "Lernt Spitzen-Export vs. Prio-Mischung: bei Einspeisung mehr Spill, "
                    "bei gutem Tag ohne Export weniger Niedrig-Prio zugunsten längerer Anker-Laufzeit."
                ),
                "formula": (
                    "record_peak_balance_tick + Abend-EMA → peak_spill_boost / "
                    "anchor_conserve_bias"
                ),
                "sources": [
                    "steering_learning.today.peak_*",
                    "peak_balance_learning",
                ],
                "module": "peak_balance_learning",
                "thresholds": "Abend-Run + Tick bei PV-Spitze 09–16 Uhr",
            },
            group="learning",
            x=X0,
            y=2070,
        )
    )
    try:
        from control_algorithm.soc_priority_neuron import (
            is_soc_priority_neuron_enabled,
            is_soc_priority_shadow,
            soc_prio_margin_pct,
        )

        spn = state.get("soc_priority_neuron") or {}
        spn_margin = soc_prio_margin_pct(state, config) if spn.get("ok") else 5.0
        spn_kw = float(spn.get("recommended_continuous_kw") or 0)
        spn_on = len(spn.get("recommended_on_ips") or [])
        spn_headroom = spn.get("soc_headroom_pct")
        spn_binding = spn.get("binding_checkpoint")
        spn_shadow = is_soc_priority_shadow(config, state)
        spn_enabled = is_soc_priority_neuron_enabled(config)
        spn_rollout = (state.get("soc_priority_neuron") or {}).get("rollout") or {}
        spn_days_left = int(spn_rollout.get("days_remaining") or 0)
    except Exception:
        spn = {}
        spn_margin = 5.0
        spn_kw = 0.0
        spn_on = 0
        spn_headroom = None
        spn_binding = None
        spn_shadow = True
        spn_enabled = False
    spn_today = sl_today
    spn_lit = (
        spn_enabled
        and bool(spn.get("ok"))
        and (
            spn_on > 0
            or spn_binding
            or int(spn_today.get("soc_prio_miss_ticks") or 0) > 0
            or bool((state.get("steering_learning") or {}).get("last_soc_prio_note"))
        )
    )
    nodes.append(
        _node(
            id="lehrneuron_soc_prio",
            label="Lehrneuron SoC-Priorität",
            kind="gate",
            cadence="tick",
            route="on" if spn_lit and not spn_shadow else "idle",
            active=spn_enabled,
            detail=(
                f"{spn_kw:.1f} kW · {spn_on} ON · "
                f"Marge {spn_margin:.0f} % · "
                f"Headroom {spn_headroom if spn_headroom is not None else '?'}"
                + (
                    f" · Shadow {spn_days_left}d"
                    if spn_shadow
                    else " · Live"
                )
            )[:110],
            live={
                "enabled": spn_enabled,
                "shadow": spn_shadow,
                "shadow_days_remaining": spn_days_left,
                "rollout_phase": spn_rollout.get("phase"),
                "shadow_since": spn_rollout.get("shadow_since"),
                "live_since": spn_rollout.get("live_since"),
                "recommended_kw": spn_kw,
                "on_count": spn_on,
                "recommended_on_ips": (spn.get("recommended_on_ips") or [])[:8],
                "margin_pct": spn_margin,
                "soc_headroom_pct": spn_headroom,
                "binding_checkpoint": spn_binding,
                "would_pause_ips": (spn.get("would_pause_ips") or [])[:6],
                "would_resume_ips": (spn.get("would_resume_ips") or [])[:6],
                "soc_prio_miss_ticks": spn_today.get("soc_prio_miss_ticks"),
                "soc_prio_tight_ticks": spn_today.get("soc_prio_tight_ticks"),
                "last_soc_prio_note": (state.get("steering_learning") or {}).get(
                    "last_soc_prio_note"
                ),
            },
            info={
                "title": "Lehrneuron SoC-Priorität",
                "purpose": (
                    "Maximal zulässige Last nach Priorität, solange SoC-Checkpoints "
                    "mit Marge erreichbar bleiben. 14 Tage Shadow, danach automatisch "
                    "Live. Wirkt auf der Planebene: deckelt den Laufzeitplan und das "
                    "Session-Ziel – es blockiert keine Schaltungen mehr."
                ),
                "formula": (
                    "simulate_battery_horizon + gierige Prio-Suche → "
                    "recommended_on_ips / recommended_continuous_kw; "
                    "Session-Deckel = recommended_continuous_kw × Reststunden"
                ),
                "sources": [
                    "soc_priority_neuron",
                    "steering_learning.soc_prio_*",
                    "config.soc_targets",
                ],
                "module": "soc_priority_neuron + surplus_session",
                "thresholds": (
                    f"Marge {spn_margin:.0f} % · "
                    f"Shadow-Phase {spn_rollout.get('shadow_days', 14)} Tage · "
                    "Stufe: Planeingang, kein Veto"
                ),
            },
            group="learning",
            x=X0,
            y=2135,
        )
    )
    try:
        from control_algorithm.storage_band_learning import storage_band_limits
        from control_algorithm.steering_learning import get_steering_params

        band_live = storage_band_limits(config, state, live=live)
        band_params = get_steering_params(state)
    except Exception:
        band_live = {}
        band_params = {}
    band_today = sl_today
    band_active = (
        int(band_today.get("band_high_ticks") or 0) > 0
        or int(band_today.get("band_over_max_ticks") or 0) > 0
        or int(band_today.get("band_low_ticks") or 0) > 0
        or float(band_params.get("band_weather_guard") or 0) > 0.12
        or float(band_live.get("overfill_risk") or 0) > 0.15
        or bool((state.get("steering_learning") or {}).get("last_band_note"))
    )
    nodes.append(
        _node(
            id="speicher_band_neuron",
            label="Speicher-Band-Neuron",
            kind="gate",
            cadence="tick",
            route="on" if band_active else "idle",
            active=band_active,
            detail=(
                f"Floor {band_live.get('floor_pct', '—')} · "
                f"Decke {band_live.get('ceiling_pct', '—')} · "
                f"Überfüll {float(band_live.get('overfill_risk') or 0):.0%}"
            )[:110],
            live={
                **band_live,
                "band_max_target_pct": band_params.get("band_max_target_pct"),
                "band_min_cushion_pct": band_params.get("band_min_cushion_pct"),
                "band_weather_guard": band_params.get("band_weather_guard"),
                "band_consumption_guard": band_params.get("band_consumption_guard"),
                "band_low_ticks": band_today.get("band_low_ticks"),
                "band_high_ticks": band_today.get("band_high_ticks"),
                "band_over_max_ticks": band_today.get("band_over_max_ticks"),
                "band_in_target_ticks": band_today.get("band_in_target_ticks"),
                "last_band_note": (state.get("steering_learning") or {}).get(
                    "last_band_note"
                ),
            },
            info={
                "title": "Speicher-Band-Neuron",
                "purpose": (
                    "Hält den SOC im Mittelfeld (Morgen-Floor … Abend-Decke): "
                    "bei schwachem Wetter/ hohem Verbrauch Decke senken (~90 %), "
                    "Horizont heute+morgen+übermorgen, Reserve bei Unterfüllung."
                ),
                "formula": (
                    "storage_band_limits + horizon_stress → "
                    "band_max_target / band_*_guard; Tick + Abend-EMA"
                ),
                "sources": [
                    "steering_learning.band_*",
                    "pv_yield_* / consumption_estimates",
                    "storage_band_learning",
                ],
                "module": "storage_band_learning",
                "thresholds": "Floor=morning+cushion · Decke≤max_soc · 3-Tage-Horizont",
            },
            group="learning",
            x=X0,
            y=2135,
        )
    )
    # Dazugelernt + Yield-Faktor (Rand, strichlierte Anbindung)
    nodes.append(
        _node(
            id="dazugelernt",
            label="Dazugelernt",
            kind="gate",
            cadence="daily",
            route="on" if (adj_n > 0 or miss_ticks > 0 or absorb_ticks > 0) else "idle",
            active=True,
            detail=(
                f"Yield {yield_factor if yield_factor is not None else '—'} · "
                f"heute {adj_n} Anpass. / {miss_ticks} Miss / {absorb_ticks} Absorb"
            ),
            live={
                "yield_factor": yield_factor,
                "performance_ratio": pr,
                "today_miss_ticks": miss_ticks,
                "today_absorb_ticks": absorb_ticks,
                "today_band_ok_ticks": band_ok,
                "today_adjustments": adj_n,
                "learning_score": learn_score,
                "last_adjustments": (steer_payload.get("last_adjustments") or [])[:5],
                "last_note": steer_payload.get("last_note"),
            },
            info={
                "title": "Dazugelernt (Yield-Faktor)",
                "purpose": (
                    "Zeigt den Yield-/Lernfaktor und was heute dazugelernt wurde "
                    "(Miss-Ticks, Absorb, letzte Anpassungen)."
                ),
                "formula": (
                    "yield_factor ≈ performance_ratio; "
                    "heute = Miss/Absorb/Band-Ticks + last_adjustments"
                ),
                "sources": [
                    "state.yield_factor / performance_ratio",
                    "steering_learning.today",
                    "learning_status_payload",
                ],
                "module": "steering_learning + calibration",
                "thresholds": "Abend-Kalibrierung schreibt Yield/PR",
            },
            group="learning",
            x=X0,
            y=2000,
        )
    )
    nodes.append(
        _node(
            id="lernfaktor_wetter_oms",
            label="Lernfaktor Wetter OMS",
            kind="gate",
            cadence="daily",
            route="on" if oms_learn_active else "idle",
            active=oms_learn_active,
            detail=(
                f"Score {pace_score if pace_score is not None else '—'} · "
                f"Engage {pace_engage if pace_engage is not None else '—'}% / "
                f"Release {pace_release if pace_release is not None else '—'}% · "
                f"Trust {pace_trust if pace_trust is not None else '—'} · "
                f"heute OMS {oms_ticks} / Muster {override_ticks}"
            ),
            live={
                "score": pace_score,
                "engage_pct": pace_engage,
                "release_pct": pace_release,
                "pattern_trust": pace_trust,
                "oms_ticks": oms_ticks,
                "override_ticks": override_ticks,
                "last_note": fl.get("last_note"),
                "last_adjustments": (fl.get("last_adjustments") or [])[:5],
                "muster_engaged_now": muster_engaged,
            },
            info={
                "title": "Lernfaktor Wetter OMS",
                "purpose": (
                    "Abend-Lernen: wann Muster statt OMS greift (Engage/Release), "
                    "wie stark der Muster-Trust ist, und ob Muster oder OMS besser war."
                ),
                "formula": (
                    "|Pace| ≥ engage → Muster; |Pace| < release → wieder OMS; "
                    "Trust mischt Muster vs. Pace-Skala"
                ),
                "sources": ["state.forecast_pace_learning"],
                "module": "pv_pattern_estimate.run_evening_forecast_pace_learning",
                "thresholds": f"Engage/Release aus params (Default 15/10)",
            },
            group="learning",
            x=X0,
            y=2120,
        )
    )
    nodes.append(
        _node(
            id="batterie_schutz",
            label="Batterie-Schutz",
            kind="guard",
            cadence="tick",
            route="off" if bat_lock else ("on" if max_soc_prot else "idle"),
            active=bat_lock or max_soc_prot,
            detail=(
                state.get("min_soc_lock_reason")
                if bat_lock
                else (
                    state.get("max_soc_protection_reason")
                    if max_soc_prot
                    else (
                        f"SoC {soc_f}% · Min {min_soc:.0f}% · Max {max_soc:.0f}%"
                        if soc_f is not None
                        else f"Min {min_soc:.0f}% · Max {max_soc:.0f}%"
                    )
                )
            ),
            live={
                "soc_percent": soc_f,
                "min_soc_percent": min_soc,
                "max_soc_percent": max_soc,
                "min_lock_active": bat_lock,
                "max_protection_active": max_soc_prot,
            },
            info={
                "title": "Batterie-Schutz (Min/Max-SoC)",
                "purpose": (
                    "Unter Min-SoC: Notaus, Algorithmus gesperrt, Budget 0. "
                    "Über Max-SoC: alle algo-Geräte ein, kein Abschalten bis unter Max. "
                    "Dazwischen: volle Algorithmus-Kontrolle."
                ),
                "formula": (
                    "SoC < min → force pause + Sperre; Erholung ab min+1 %. "
                    "SoC ≥ max → force resume all; Freigabe am Halt-SoC (Stufe 1)"
                ),
                "sources": [
                    "live.soc_percent",
                    "config.min_soc_percent",
                    "config.max_soc_percent",
                ],
                "module": "battery_protection + check_min_soc_emergency",
                "thresholds": f"Min = {min_soc:.0f} % · Max = {max_soc:.0f} %",
            },
            group="guards",
            x=XP,
            y=300,
        )
    )
    max_soc_route = "on" if (max_soc_over or max_soc_prot) else ("idle" if not max_soc_near else "on")
    if not max_soc_over and not max_soc_near:
        max_soc_route = "idle"
    # Plan-Decke aus dem Band-Neuron; das harte Max bleibt nur Notbremse darüber
    try:
        from control_algorithm.storage_band_learning import effective_planning_max_soc

        band_max_pct = float(effective_planning_max_soc(config, state, live=live))
    except Exception:
        band_max_pct = max_soc
    nodes.append(
        _node(
            id="max_soc",
            label="Max-SoC",
            kind="guard",
            cadence="tick",
            route=max_soc_route if (max_soc_over or max_soc_prot or (max_soc_near and yes_active)) else "idle",
            active=max_soc_over or max_soc_prot or fill_urgent or (max_soc_near and yes_active),
            detail=(
                f"SoC {soc_f}% · Band {band_max_pct:.0f}% · Max {max_soc:.0f}%"
                if soc_f is not None
                else f"Band {band_max_pct:.0f}% · Max {max_soc:.0f}%"
            )[:110],
            live={
                "soc_percent": soc_f,
                "plan_band_max_pct": round(band_max_pct, 1),
                "max_soc_percent": max_soc,
                "over_max": max_soc_over,
                "fill_urgent": fill_urgent,
            },
            info={
                "title": "Max-SoC / Entlade-Vorrang",
                "purpose": (
                    "Zweistufig: die Plan-Decke aus dem Band-Neuron führt den Plan "
                    "(Last rein, bevor der Speicher am Band klebt), das harte Max "
                    "bleibt Notbremse darüber."
                ),
                "formula": (
                    "Plan-Band = effective_planning_max_soc; "
                    "hart: SoC ≥ max → Schutz-Resume, SoC > max+0,3 → over_max"
                ),
                "sources": [
                    "live.soc_percent",
                    "storage_band_learning.effective_planning_max_soc",
                    "config.max_soc_percent",
                ],
                "module": "battery_protection + fill_schedule + storage_band_learning",
                "thresholds": (
                    f"Band {band_max_pct:.0f} % (Planführung) · "
                    f"Max {max_soc:.0f} % (hard_stop)"
                ),
            },
            group="guards",
            x=XP,
            y=410,
        )
    )
    try:
        from control_algorithm.battery_protection import max_soc_release_percent

        release_soc = float(max_soc_release_percent(config))
    except Exception:
        release_soc = max(float(min_soc) + 1.0, float(max_soc) - 5.0)
    hold_released = bool(state.get("max_soc_hold_released"))
    hold_on = bool(max_soc_prot or max_soc_over)
    if hold_released:
        hold_route = "off"
    elif hold_on:
        hold_route = "on"
    else:
        hold_route = "idle"
    hold_detail = (
        f"SoC {soc_f}% · Max {max_soc:.0f}% ein · Halt {release_soc:.0f}% aus"
        if soc_f is not None
        else f"Max {max_soc:.0f}% ein · Halt {release_soc:.0f}% aus"
    )
    if hold_released:
        hold_detail += " · aus bis Max"
    elif hold_on:
        hold_detail += " · Latch an"
    nodes.append(
        _node(
            id="stufe1_halteband",
            label="Stufe 1 Halt-Band",
            kind="guard",
            cadence="tick",
            route=hold_route,
            active=hold_on or hold_released,
            detail=hold_detail[:110],
            live={
                "soc_percent": soc_f,
                "max_soc_percent": max_soc,
                "max_soc_release_percent": round(release_soc, 1),
                "latch_active": bool(state.get("max_soc_override_active") or max_soc_prot),
                "hold_released": hold_released,
                "aus_bis_max": hold_released,
            },
            info={
                "title": "Stufe 1 Halt-Band",
                "purpose": (
                    "Nach dem harten Maximum bleiben die Verbraucher an, "
                    "bis der Speicher auf den eingestellten Halt fällt. "
                    "Dort alles aus, bis das Maximum wieder erreicht wird."
                ),
                "formula": "SoC ≥ Max → alle ein; SoC ≤ Halt → alle aus bis Max",
                "sources": [
                    "live.soc_percent",
                    "config.max_soc_percent",
                    "config.max_soc_release_percent",
                    "state.max_soc_override_active",
                    "state.max_soc_hold_released",
                ],
                "module": "fill_schedule.apply_max_soc_override_latch + battery_protection",
                "thresholds": (
                    f"Ein ab {max_soc:.0f} % · Aus bei {release_soc:.0f} % · "
                    "Kalibrierung = eingestellter Halt, kein Lern-Offset"
                ),
            },
            group="guards",
            x=XP,
            y=530,
        )
    )

    surplus_route = "on" if yes_active else ("off" if no_active else "idle")
    nodes.append(
        _node(
            id="ueberschuss",
            label="Überschuss verfügbar?",
            kind="decision",
            cadence="hourly",
            route=surplus_route,
            active=True,
            detail=(
                f"Ja · heute {today_kwh} kWh ({today_kw} kW)"
                if yes_active
                else f"Nein · heute {today_kwh} kWh"
            )[:110],
            live={
                "has_surplus": has_surplus,
                "today_surplus_kwh": today_kwh,
                "today_surplus_kw": today_kw,
                "week_budget_kw": budget_kw,
                "week_surplus_kwh": surplus_kwh,
                "export_kw": round(export_kw, 2),
            },
            info={
                "title": "Überschuss verfügbar",
                "purpose": (
                    "Zentrale Ja/Nein für gesteuerte Last. Maßgeblich ist der "
                    "Tages-Überschuss; das 7-Tage-Budget wirkt nur als Deckel."
                ),
                "formula": "heute = Akku + Rest-PV + Hydro − Grundlast − Abendreserve",
                "sources": ["state.today_surplus", "balance_projection"],
                "module": "energy_balance.compute_today_surplus",
                "thresholds": "Wochendeckel: miner_surplus_kwh",
            },
            group="core",
            x=XD,
            y=260,
        )
    )

    # Planebene: Tages-Überschuss (Tageszahl statt Wochenbudget)
    today_ok = bool(today_surplus.get("ok"))
    pace_scale = _num(today_surplus.get("pace_scale"), 3)
    nodes.append(
        _node(
            id="tages_ueberschuss",
            label="Tages-Überschuss",
            kind="compact",
            cadence="hourly",
            route="on" if (today_ok and today_kwh > 0.3) else "idle",
            active=today_ok,
            detail=(
                f"{today_kwh} kWh heute · {today_kw} kW"
                + (f" · Pace ×{pace_scale}" if pace_scale and pace_scale != 1.0 else "")
                + (
                    f" · gedeckelt ({today_surplus.get('capped_by')})"
                    if today_surplus.get("capped_by")
                    else ""
                )
            )[:110],
            live={
                "kwh": today_kwh,
                "continuous_kw": today_kw,
                "rest_pv_kwh": _num(today_surplus.get("rest_pv_kwh")),
                "rest_base_kwh": _num(today_surplus.get("rest_base_kwh")),
                "reserve_soc_pct": _pct(today_surplus.get("reserve_soc_pct")),
                "pace_scale": pace_scale,
                "pace_deviation_pct": _pct(today_surplus.get("pace_deviation_pct")),
                "capped_by": today_surplus.get("capped_by"),
            },
            info={
                "title": "Tages-Überschuss",
                "purpose": (
                    "Menge, die heute noch übrig bleibt. Ziel der Überschuss-Session. "
                    "Das 7-Tage-Budget deckelt nur, damit der Morgen-Checkpoint hält."
                ),
                "formula": "Akku + Rest-PV×Pace + Hydro − Grundlast − Abendreserve",
                "sources": [
                    "state.today_surplus",
                    "curve_plan.steering_goal.floor_eve_pct",
                    "pv_safety_watchdog.compute_pv_pace_signals",
                ],
                "module": "energy_balance.compute_today_surplus",
                "thresholds": "Stufe: plan_regular · formt das Session-Ziel",
            },
            group="core",
            x=XC,
            y=520,
        )
    )

    # Planführung: laufender Überschuss-Zug
    nodes.append(
        _node(
            id="ueberschuss_session",
            label="Überschuss-Session",
            kind="gate",
            cadence="tick",
            route=(
                "idle"
                if (not session_active or session_suspended_by)
                else ("on" if session_rest_kwh > 0 else "idle")
            ),
            active=bool(session_active and not session_suspended_by),
            detail=(
                (
                    f"pausiert ({session_suspended_by}) · noch {session_rest_kwh} kWh"
                    if session_suspended_by
                    else f"{session_done_kwh} von {session_goal_kwh} kWh · noch {session_rest_kwh}"
                )
                if session_active
                else f"kein Zug ({session.get('end_reason') or 'nicht gestartet'})"
            )[:110],
            live={
                "active": session_active,
                "goal_kwh": session_goal_kwh,
                "delivered_kwh": session_done_kwh,
                "remaining_kwh": session_rest_kwh,
                "suspended_by": session_suspended_by or None,
                "devices": [str(x) for x in (session.get("devices") or [])][:6],
                "goal_capped_by_neuron": bool(session.get("goal_capped_by_neuron")),
            },
            info={
                "title": "Überschuss-Session",
                "purpose": (
                    "Einmal gestarteter Zug wird gehalten bis die Menge abgebaut "
                    "ist. Weiche Schichten drosseln, Risiko-Schichten pausieren, "
                    "nur Notbremsen beenden."
                ),
                "formula": "Rest = goal_kwh − delivered_kwh (Menge, nicht Laufzeit)",
                "sources": ["state.surplus_session", "state.today_surplus"],
                "module": "surplus_session",
                "thresholds": "Stufe: plan_session · über Floor/Reserve",
            },
            group="core",
            x=XD,
            y=420,
        )
    )

    # Yes branch
    nodes.append(
        _node(
            id="export_vermeidung",
            label="Export-Vermeidung",
            kind="gate",
            cadence="tick",
            route="on"
            if yes_active and (export_guard or export_kw >= 0.05)
            else on_path
            if yes_active
            else "idle",
            active=yes_active and (export_guard or export_kw >= 0.05),
            detail=(
                f"Guard {state.get('export_guard_floor_kw')} kW"
                if export_guard
                else f"Export {export_kw:.2f} kW"
            ),
            live={
                "export_guard_active": export_guard,
                "export_guard_floor_kw": _f(state.get("export_guard_floor_kw"), 2),
            },
            info={
                "title": "Export-Vermeidung (Akteur 1)",
                "purpose": "Bei Export Mindestlast halten.",
                "formula": "export_guard_floor sticky",
                "sources": ["state.export_guard_*"],
                "module": "decision_arbitrator",
                "thresholds": "export ≥ 0.05 kW",
            },
            group="yes_branch",
            x=XY,
            y=40,
        )
    )
    nodes.append(
        _node(
            id="near_term",
            label="Near-Term / Proaktiv",
            kind="gate",
            cadence="tick",
            route="on"
            if yes_active
            and (
                binding in ("near_term_discharge", "proactive_discharge", "max_soc")
                or (proactive_kw or 0) > 0.5
            )
            else on_path
            if yes_active
            else "idle",
            active=yes_active
            and (
                binding in ("near_term_discharge", "proactive_discharge", "max_soc")
                or (proactive_kw or 0) > 0.5
            ),
            detail=(f"proaktiv {proactive_kw} kW" if proactive_kw else binding or "—")[:110],
            live={"proactive_discharge_kw": proactive_kw, "binding": binding or None},
            info={
                "title": "Near-Term / Proaktiv",
                "purpose": "Früh entladen vor Max-SoC / schönem Morgen.",
                "formula": "proactive_discharge_kw aus Bilanz",
                "sources": ["near_term_plan"],
                "module": "energy_balance + load_scheduler",
                "thresholds": "> 0.5 kW",
            },
            group="yes_branch",
            x=XY,
            y=150,
        )
    )
    nodes.append(
        _node(
            id="prio_laufzeit",
            label="Prio-Laufzeit (≥24 h?)",
            kind="gate",
            cadence="tick",
            route=on_path if (yes_active or hold_active) else "idle",
            active=yes_active or hold_active,
            detail=(prio_plan.get("summary") or f"Anker ~{anchor_h} h")[:110],
            live={
                "anchor_hours": anchor_h,
                "second_device": second_prio,
                "preferred_kw": prio_plan.get("preferred_continuous_kw"),
            },
            info={
                "title": "Prioritäts-Laufzeit",
                "purpose": "Höchste Prio möglichst lange; ≥24 h → zweite Stufe.",
                "formula": "h_i = min(hours_left, remaining/P_i)",
                "sources": ["priority_runtime_plan"],
                "module": "priority_runtime",
                "thresholds": "Anker ≥ 24 h",
            },
            group="yes_branch",
            x=XY,
            y=260,
        )
    )
    nodes.append(
        _node(
            id="fuellplan",
            label="Füllplan Max-SoC",
            kind="gate",
            cadence="tick",
            route="on" if (fill_flex.get("should_start_now") or fill_urgent) else "idle",
            active=bool(fill.get("ok") and fill_flex.get("planned_on")) or fill_urgent,
            detail=str(fill_flex.get("reason") or "—")[:110],
            live={
                "planned_on": fill_flex.get("planned_on"),
                "should_start_now": fill_flex.get("should_start_now"),
            },
            info={
                "title": "Füllplan",
                "purpose": "Flex-EIN gegen Max-SoC / PV-Ende.",
                "formula": "ETA Max-SoC mit/ohne Flex",
                "sources": ["fill_schedule"],
                "module": "fill_schedule",
                "thresholds": "should_start_now",
            },
            group="yes_branch",
            x=XY,
            y=370,
        )
    )
    nodes.append(
        _node(
            id="fuell_dringlichkeit",
            label="Füll-Dringlichkeit",
            kind="gate",
            cadence="tick",
            route="on" if fill_urgent else "idle",
            active=fill_urgent,
            detail=(
                "Max-SoC-Urgency – überschreibt Morgen-Floor/Commitment"
                if fill_urgent
                else "keine Urgency"
            )[:110],
            live={
                "fill_urgency_active": fill_urgent,
                "over_max": bool(fill.get("over_max")),
                "capacity_limited": bool(fill.get("capacity_limited")),
                "hours_to_max": fill.get("hours_to_max"),
            },
            info={
                "title": "Füll-Dringlichkeit (Max-SoC)",
                "purpose": (
                    "Wenn der Speicher über Max läuft oder in Stunden voll ist: "
                    "Last muss EIN, selbst gegen Morgen-Floor und Anti-Flatter-AUS."
                ),
                "formula": "over_max OR capacity_limited OR hours_to_max ≤ Schwellwert",
                "sources": ["state.fill_urgency_active", "fill_schedule"],
                "module": "fill_schedule.compute_fill_urgency + switch_commitment",
                "thresholds": "hours_to_max / excess_kw aus Config",
            },
            group="yes_branch",
            x=XY,
            y=480,
        )
    )
    nodes.append(
        _node(
            id="wetter_durchlauf",
            label="Wetter-Durchlauf",
            kind="gate",
            cadence="tick",
            route="on" if run_through_active else "idle",
            active=run_through_active,
            detail=(
                f"Floor {run_through.get('floor_kw')} kW"
                + (" · wie Export" if run_through.get("treat_as_export") else "")
                if run_through_active
                else "kein Gutwetter-Boden"
            )[:110],
            live=dict(run_through) if run_through else {},
            info={
                "title": "Wetter-Durchlauf (Budget-Boden)",
                "purpose": (
                    "Bei starkem Tag / hohem SoC: Mindest-Budget, kein Abschalten nur weil "
                    "Horizont-Budget 0 ist. Kann wie Export behandelt werden."
                ),
                "formula": "strong_day/high_soc → floor_kw ≥ Anker; treat_as_export",
                "sources": ["live.pv_kw", "live.soc", "today_pv"],
                "module": "load_scheduler.weather_run_through_floor",
                "thresholds": "PV-Tag ≥ 65% Perfekt oder SoC ≥ max−5",
            },
            group="yes_branch",
            x=XY,
            y=590,
        )
    )
    nodes.append(
        _node(
            id="spaetstart",
            label="Spätstart",
            kind="gate",
            cadence="tick",
            route="off" if late_active else "idle",
            active=late_active,
            detail=str(late.get("summary") or "nicht aktiv")[:110],
            live={"defer": late.get("defer"), "excess_kwh": late.get("excess_kwh")},
            info={
                "title": "Spätstart",
                "purpose": "Zusatzgeräte warten; Anker zuerst.",
                "formula": "defer bei excess + offenem Fenster",
                "sources": ["late_start_plan"],
                "module": "late_start",
                "thresholds": "excess ≥ 0.4 kWh",
            },
            group="yes_branch",
            x=XY,
            y=700,
        )
    )
    drain_ok = bool(drain.get("ok"))
    drain_policy = str(drain.get("policy") or "")
    drain_blocks = bool(drain.get("gate_active")) and not drain.get("allowed_ips")
    drain_g = drain.get("g_kwh")
    drain_must_empty = _f(drain.get("must_empty_kwh"), 2) or 0.0
    try:
        from control_algorithm.steering_learning import get_steering_params

        drain_params = get_steering_params(state)
    except Exception:
        drain_params = {}
    nodes.append(
        _node(
            id="laufreserve",
            label="Laufreserve g",
            kind="gate",
            cadence="tick",
            route="off" if drain_blocks else ("on" if drain_ok else "idle"),
            active=drain_blocks,
            detail=(
                (
                    f"g {_f(drain_g, 1)} kWh · "
                    f"frei {len(drain.get('allowed_ips') or [])} Gerät(e)"
                )
                if drain.get("gate_active")
                else str(drain.get("reason") or drain_policy or "nicht zuständig")
            ),
            live={
                "gate_active": drain.get("gate_active"),
                "g_kwh": drain_g,
                "available_kwh": drain.get("available_kwh"),
                "need_kwh": drain.get("need_kwh"),
                "m_risk_kwh": drain.get("m_risk_kwh"),
                "battery_is_source": drain.get("battery_is_source"),
                "break_even_at": drain.get("break_even_at"),
                "allowed_ips": drain.get("allowed_ips"),
            },
            info={
                "title": "Laufreserve g",
                "purpose": (
                    "Einzige Nachtschranke: Geräte laufen aus dem Speicher nur, "
                    "wenn er bis zum PV-Break-even trägt. Abendstopp und "
                    "Frühstart sind dieselbe Ungleichung."
                ),
                "formula": "g = (SoC−Floor)/100·C − ∫(B+P−H−G) − m_risk",
                "sources": ["morning_drain_plan"],
                "module": "morning_drain",
                "thresholds": "g ≥ Totband max(1 % C, 0.05 · P)",
            },
            group="yes_branch",
            x=XY,
            y=760,
        )
    )
    nodes.append(
        _node(
            id="morgen_drain",
            label="Morgen-Drain",
            kind="gate",
            cadence="tick",
            route="on" if drain_must_empty > 0 else "idle",
            active=drain_must_empty > 0,
            detail=str(drain.get("summary") or "nicht aktiv")[:110],
            live={
                "policy": drain_policy or None,
                "must_empty_kwh": drain.get("must_empty_kwh"),
                "target_soc_percent": drain.get("target_soc_percent"),
                "headroom_soc_percent": drain.get("headroom_soc_percent"),
                "start_at": drain.get("start_at"),
                "break_even_at": drain.get("break_even_at"),
                "accepted_curtail_kwh": drain.get("accepted_curtail_kwh"),
            },
            info={
                "title": "Morgen-Drain",
                "purpose": (
                    "Headroom wird morgens freigemacht statt abends, weil der "
                    "Nachtverbrauch dann bekannt statt prognostiziert ist."
                ),
                "formula": "s* = min(max(s_head, s_floor), SoC); Floor gewinnt immer",
                "sources": ["morning_drain_plan"],
                "module": "morning_drain",
                "thresholds": "must_empty > 0 und Break-even vorhanden",
            },
            group="yes_branch",
            x=XY,
            y=820,
        )
    )
    nodes.append(
        _node(
            id="drain_neuron",
            label="Neuron Morgen-Drain",
            kind="neuron",
            cadence="abend",
            route="on" if drain_ok else "idle",
            active=drain_ok,
            detail=str(
                (state.get("steering_learning") or {}).get("last_morning_drain_note")
                or "lernt Nachtpuffer und PV-Vertrauen"
            )[:110],
            live={
                k: drain_params.get(k)
                for k in (
                    "drain_base_load_bias",
                    "drain_night_risk_frac",
                    "drain_floor_margin_kwh_pct",
                    "drain_pv_trust",
                    "drain_device_kw_bias",
                )
            },
            info={
                "title": "Neuron Morgen-Drain",
                "purpose": (
                    "Justiert Grundlast, Nachtpuffer und PV-Vertrauen. Floor "
                    "gerissen hebt den Puffer, mehrere Tage ohne Frühstart "
                    "senken ihn."
                ),
                "formula": "EMA mit alpha = 0.15 · min(1, Tage/14)",
                "sources": ["steering_learning.params", "morning_drain_plan"],
                "module": "morning_drain",
                "thresholds": "harte Grenzen in PARAM_BOUNDS",
            },
            group="yes_branch",
            x=XY,
            y=880,
        )
    )
    nodes.append(
        _node(
            id="anker_streckung",
            label="Anker-Streckung",
            kind="gate",
            cadence="tick",
            route="off" if stretch_active else "idle",
            active=stretch_active,
            detail=(
                f"Hydro-Aus ~{stretch.get('soc_hydro_off_percent')} %"
                if stretch.get("soc_hydro_off_percent") is not None
                else "aus"
            ),
            live={
                "active": stretch_active,
                "anchor_hours": stretch.get("anchor_hours"),
                "soc_hydro_off": stretch.get("soc_hydro_off_percent"),
            },
            info={
                "title": "Anker-Streckung",
                "purpose": "Flex aus, Hydro länger.",
                "formula": str(stretch.get("formula") or "Reserve bis PV-Start"),
                "sources": ["anchor_stretch"],
                "module": "anchor_stretch",
                "thresholds": "active",
            },
            group="yes_branch",
            x=XY,
            y=810,
        )
    )

    # No branch: Wochenplaner + 7 Tage, dann Nacht-Sperre / Last reduzieren
    week_active = bool(no_active)
    week_route = (
        "off" if (no_active and not tomorrow_ok) else ("on" if hold_active else "idle")
    )
    week_plan = (
        state.get("energy_week_plan")
        if isinstance(state.get("energy_week_plan"), dict)
        else {}
    )
    nodes.append(
        _node(
            id="wochenplaner",
            label="Wochenplaner",
            kind="decision",
            cadence="hourly",
            route=week_route,
            active=week_active,
            detail=(
                f"{len(horizon_rows)} Tage · "
                + ("Reserve ok" if tomorrow_ok else "knapp")
            )[:110],
            live={
                "horizon_days": len(horizon_rows),
                "tomorrow_ok": tomorrow_ok,
                "plan_max_soc_percent": week_plan.get("plan_max_soc_percent")
                or band_live.get("ceiling_pct"),
                "plan_min_soc_percent": week_plan.get("plan_min_soc_percent")
                or band_live.get("floor_pct"),
                "band_overfill_risk": (
                    week_plan.get("band_overfill_risk")
                    if week_plan.get("band_overfill_risk") is not None
                    else band_live.get("overfill_risk")
                ),
                "band_underfill_risk": (
                    week_plan.get("band_underfill_risk")
                    if week_plan.get("band_underfill_risk") is not None
                    else band_live.get("underfill_risk")
                ),
                "band_note": week_plan.get("band_note")
                or (state.get("steering_learning") or {}).get("last_band_note"),
            },
            info={
                "title": "Wochenplaner",
                "purpose": (
                    "7-Tage-Horizont als Baum: pro Wochentag Produktionsblöcke, "
                    "„Wie schaut's aus?“ mit Ja/Nein, dann nächster Tag. "
                    "SOC-Cap und Miner-Budget nutzen Speicher-Band-Decke/Floor."
                ),
                "formula": "planning_horizon_days (Default 7) + band plan_max/plan_min",
                "sources": [
                    "horizon_projection",
                    "weather_context",
                    "checkpoints",
                    "speicher_band_neuron",
                ],
                "module": "energy_balance + weather_survival + storage_band_learning",
                "thresholds": "ein knapper Tag → Abzweig Last reduzieren",
            },
            group="horizon",
            x=HORIZ_X,
            y=HORIZ_SUPPORT_Y,
        )
    )
    _append_horizon_week_tree(
        nodes,
        horizon_rows=horizon_rows,
        horiz_proj=horiz_proj,
        no_active=no_active,
        hold_active=hold_active,
        tomorrow_ok=tomorrow_ok,
        tomorrow_detail=tomorrow_detail,
        x_base=HORIZ_X,
        y_base=Y_HORIZ,
        x_step=HORIZ_STEP,
    )
    nodes.append(
        _node(
            id="morgen_floor",
            label="Morgen-Floor",
            kind="guard",
            cadence="tick",
            route=(
                "on"
                if daytime_pv_hold and morning_threat
                else ("off" if morning_shed or morning_threat else "idle")
            ),
            active=morning_threat or daytime_pv_hold,
            detail=(
                (
                    "Shed ausgesetzt – Tages-PV hält"
                    if daytime_pv_hold and morning_threat
                    else (
                        f"gefährdet vs. {morning_soc}%"
                        if morning_threat
                        else f"Ziel ≥ {morning_soc}%"
                    )
                )
            )[:110],
            live={
                "threatened": morning_threat,
                "shed": morning_shed,
                "daytime_pv_hold": daytime_pv_hold,
                "morning_soc_percent": morning_soc,
                "fill_urgent_blocks_shed": fill_urgent,
            },
            info={
                "title": "Morgen-Floor",
                "purpose": (
                    "Nächster Morgen-Checkpoint unter Floor → niedrige Prio abwerfen. "
                    "Füll-Dringlichkeit und starke Tages-PV können den Shed aussetzen. "
                    "Unabhängig vom Wochenplaner."
                ),
                "formula": "morning_floor_threatened; morning_shed = threat ∧ ¬fill_urgent ∧ ¬daytime_pv_hold",
                "sources": ["state.morning_floor_threatened", "state.daytime_pv_hold"],
                "module": "load_scheduler.morning_floor_threatened",
                "thresholds": f"Floor {morning_soc}% + Margin; Lookahead ~18 h",
            },
            group="no_branch",
            x=XN,
            y=200,
        )
    )
    nodes.append(
        _node(
            id="nacht_sperre",
            label="Nacht-Sperre",
            kind="guard",
            cadence="tick",
            route="off" if night else "idle",
            active=night,
            detail=str(state.get("night_lock_reason") or ("aktiv" if night else "aus"))[:110],
            live={"night_lock_active": night},
            info={
                "title": "Nacht-Sperre",
                "purpose": "Nachts Last pausieren.",
                "formula": "night_lock_pause_actions",
                "sources": ["state.night_lock_*"],
                "module": "load_scheduler",
                "thresholds": "aktiv → Pause",
            },
            group="no_branch",
            x=XN,
            y=320,
        )
    )
    nodes.append(
        _node(
            id="last_reduzieren",
            label="Last reduzieren",
            kind="gate",
            cadence="tick",
            route=off_path if shed_active else "idle",
            active=shed_active,
            detail=(
                state.get("night_lock_reason")
                or state.get("min_soc_lock_reason")
                or binding
                or "Abwurf"
            )[:110],
            live={"pause_count": len(pauses)},
            info={
                "title": "Last reduzieren",
                "purpose": "Pause nach Priorität bei knapper Energie.",
                "formula": "niedrige Prio zuerst",
                "sources": ["planned_actions"],
                "module": "load_scheduler",
                "thresholds": "Nacht/Survival/Min-SoC",
            },
            group="no_branch",
            x=XN,
            y=440,
        )
    )
    partei1_active = yes_active
    partei2_active = bool(shed_active or night or bat_lock or (no_active and not tomorrow_ok))
    nodes.append(
        _node(
            id="partei_1",
            label="Partei 1 – Planführung",
            kind="gate",
            cadence="tick",
            route=on_path if partei1_active else "idle",
            active=partei1_active,
            detail=(
                f"Session hält · noch {session_rest_kwh} kWh"
                if (session_active and not session_suspended_by)
                else "Export vermeiden / mehr Last"
            )[:110],
            live={
                "export_guard_active": export_guard,
                "export_kw": round(export_kw, 2),
                "session_active": session_active,
                "session_remaining_kwh": session_rest_kwh,
            },
            info={
                "title": "Partei 1 – Planführung",
                "purpose": (
                    "Fordert Last an: laufende Überschuss-Session, Export-Vermeidung, "
                    "Füllplan gegen das Plan-Band."
                ),
                "formula": "Stufe plan_session, darunter plan_regular",
                "sources": ["ueberschuss_session", "export_vermeidung", "near_term"],
                "module": "control_priority + surplus_session",
                "thresholds": "schlägt Reserve/Floor, verliert gegen Notbremsen",
            },
            group="yes_branch",
            x=XY,
            y=920,
        )
    )
    nodes.append(
        _node(
            id="partei_2",
            label="Partei 2 – Reserve und Risiko",
            kind="gate",
            cadence="tick",
            route="off" if partei2_active else "idle",
            active=partei2_active,
            detail=(
                f"bremst Session ({session_suspended_by})"
                if session_suspended_by
                else "Reserve / Nacht / weniger Last"
            )[:110],
            live={
                "night_lock_active": night,
                "shed_active": shed_active,
                "session_suspended_by": session_suspended_by or None,
            },
            info={
                "title": "Partei 2 – Reserve und Risiko",
                "purpose": (
                    "Will Last drosseln: Morgen-Floor und Anker-Reserve drosseln nur, "
                    "PV-Safety und Anomalie pausieren die Session, Min-SoC und "
                    "Nacht-Sperre beenden sie."
                ),
                "formula": "Stufen soft_reserve (drosseln) und hard_stop (beenden)",
                "sources": ["nacht_sperre", "last_reduzieren", "pv_sicherheit"],
                "module": "control_priority",
                "thresholds": "soft_reserve verliert gegen plan_session",
            },
            group="no_branch",
            x=XN,
            y=560,
        )
    )

    arb_route = global_route if global_route != "idle" else (on_path if on_path != "idle" else off_path)
    nodes.append(
        _node(
            id="schiedsrichter",
            label="Schiedsrichter (Prioritätsleiter)",
            kind="gate",
            cadence="tick",
            route=arb_route,
            active=bool(actions) or export_guard or bat_lock or night or partei1_active or partei2_active,
            detail=(
                f"{len(resumes)}× EIN · {len(pauses)}× AUS"
                + (f" · {control_owner.get('label')}" if control_owner.get("label") else "")
            )[:110],
            live={
                "resume_count": len(resumes),
                "pause_count": len(pauses),
                "control_owner": control_owner.get("level"),
                "control_owner_label": control_owner.get("label"),
                "ladder": list(PRIORITY_LADDER),
            },
            info={
                "title": "Schiedsrichter – eine Leiter, ein Gewinner",
                "purpose": (
                    "Eine einzige Prioritätsleiter statt fest verdrahteter Reihenfolgen. "
                    "Jede Entscheidung landet in state['arbiter_trace']."
                ),
                "formula": " > ".join(PRIORITY_LADDER),
                "sources": ["control_priority", "switch_commitment._same_tick_arbiter"],
                "module": "control_priority",
                "thresholds": "hard_stop schlägt plan_session schlägt soft_reserve",
            },
            group="filter",
            x=XF,
            y=160,
        )
    )
    prio_route = arb_route
    if prio_violations:
        prio_route = "off"
    elif prio_force_resume:
        prio_route = "on"
    nodes.append(
        _node(
            id="prio_invariante",
            label="Prio-Reihenfolge",
            kind="gate",
            cadence="tick",
            route=prio_route if prio_active else arb_route,
            active=bool(actions) or prio_active or export_guard or bat_lock or night,
            detail=(
                f"{len(prio_violations)} Verletzung(en) – Flex nicht ohne Anker"
                if prio_violations
                else "höhere Stufe zuerst"
            )[:110],
            live={
                "violations": [v.get("ip") for v in prio_violations[:6]],
                "force_resume": prio_force_resume,
            },
            info={
                "title": "Prioritäts-Invariante",
                "purpose": (
                    "Niedrigere Stufe darf nicht laufen, solange höhere Stufe aus ist. "
                    "Force-Pause Flex + Force-Resume Anker – sonst Flattern. "
                    "Erster Schritt in plan_load_actions (vor Schiedsrichter)."
                ),
                "formula": "find_priority_violations → pause low, resume high (force)",
                "sources": ["planner.find_priority_violations"],
                "module": "planner + load_scheduler",
                "thresholds": "sofort, auch gegen Min-Laufzeit",
            },
            group="filter",
            x=XF,
            y=260,
        )
    )
    nodes.append(
        _node(
            id="min_runtime",
            label="Min-Laufzeit",
            kind="gate",
            cadence="tick",
            route=arb_route,
            active=bool(actions) or export_guard or bat_lock or night,
            detail=f"min {config.get('min_device_runtime_hours', '—')} h",
            live={"min_device_runtime_hours": config.get("min_device_runtime_hours")},
            info={
                "title": "Min-Laufzeit",
                "purpose": "Keine zu kurzen EIN-Fenster.",
                "formula": "apply_device_runtime_guards",
                "sources": ["config.min_device_runtime_hours"],
                "module": "load_scheduler",
                "thresholds": "Min-Stunden",
            },
            group="filter",
            x=XF,
            y=360,
        )
    )
    nodes.append(
        _node(
            id="min_off",
            label="Min-Aus / Ruhezeit",
            kind="gate",
            cadence="tick",
            route="off" if min_off_blocking else arb_route,
            active=bool(actions) or min_off_blocking or export_guard or bat_lock or night,
            detail=(
                f"Rest {min_off_remain:.2f} h"
                if min_off_blocking
                else f"min {min_off_h if min_off_h is not None else '—'} h"
            ),
            live={
                "min_device_off_hours": min_off_h,
                "remaining_h": round(min_off_remain, 2),
            },
            info={
                "title": "Mindest-Auszeit",
                "purpose": "Nach Pause nicht sofort wieder EIN (Anti-Flap).",
                "formula": "should_defer_resume_for_stability / off_stability_remaining_h",
                "sources": ["config.min_device_off_hours", "device_runtime"],
                "module": "load_scheduler.apply_device_runtime_guards",
                "thresholds": "Rest > 0 → Resume blockiert (außer urgent/force)",
            },
            group="filter",
            x=XF,
            y=460,
        )
    )
    nodes.append(
        _node(
            id="switch_commitment",
            label="Anti-Flatter Commitment",
            kind="gate",
            cadence="tick",
            route=arb_route,
            active=bool(actions) or export_guard or bat_lock or night,
            detail="Schalt-Bindung",
            live={},
            info={
                "title": "Switch-Commitment",
                "purpose": "Stabilisiert EIN/AUS gegen Flattern.",
                "formula": "apply_switch_commitment_guards",
                "sources": ["switch_commitment"],
                "module": "switch_commitment",
                "thresholds": "Commit-Fenster; Fill-Urgency darf AUS überschreiben",
            },
            group="filter",
            x=XF,
            y=560,
        )
    )
    # Tages-Einschaltlimit aus Runtime
    on_limit_hit = False
    on_limit_detail = "frei"
    try:
        from control_algorithm.switch_commitment import (
            hard_daily_on_limit_enabled,
            normalize_max_on_per_day,
            on_count_today,
            runtime_entry,
        )

        max_on_cfg = normalize_max_on_per_day(config.get("max_device_on_per_day", 3))
        if hard_daily_on_limit_enabled(config) and max_on_cfg > 0:
            locked = []
            for d in config.get("devices") or []:
                ip = str(d.get("ip") or "")
                if not ip:
                    continue
                cnt = on_count_today(runtime_entry(state, ip))
                if cnt >= max_on_cfg:
                    locked.append(f"{ip.split('.')[-1]}:{cnt}")
            if locked:
                on_limit_hit = True
                on_limit_detail = f"gesperrt {', '.join(locked[:4])} (max {max_on_cfg})"
            else:
                on_limit_detail = f"max {max_on_cfg} EIN/Gerät/Tag"
    except Exception:
        pass
    nodes.append(
        _node(
            id="switch_limit",
            label="Schalt-Limit (3× EIN/Tag)",
            kind="gate",
            cadence="tick",
            route="off" if on_limit_hit else arb_route,
            active=bool(actions) or on_limit_hit or export_guard or bat_lock or night,
            detail=on_limit_detail[:110],
            live={
                "hard_limit": bool(config.get("hard_daily_on_limit", True)),
                "max_on": config.get("max_device_on_per_day", 3),
                "blocked": on_limit_hit,
                "switch_risk": state.get("switch_risk") or {},
            },
            info={
                "title": "Tages-Einschaltlimit",
                "purpose": (
                    "Max. N Einschaltungen pro Gerät und Kalendertag, "
                    "danach harte Sperre bis Mitternacht. "
                    "Blockierte Resumes speisen das Risiko-Neuron."
                ),
                "formula": "on_count_today ≥ max_device_on_per_day → Resume block",
                "sources": [
                    "config.max_device_on_per_day",
                    "config.hard_daily_on_limit",
                    "device_runtime.on_count_today",
                ],
                "module": "switch_commitment.apply_switch_commitment_guards",
                "thresholds": "Nur Notfall darf Sperre brechen",
            },
            group="filter",
            x=XF,
            y=610,
        )
    )
    # Manuelle Fremdsteuerung: Hold-Countdown
    manual_holds: List[Dict[str, Any]] = []
    manual_hold_detail = "frei"
    manual_hold_active = False
    try:
        from control_algorithm.manual_override import (
            active_manual_holds,
            normalize_manual_override_hold_hours,
        )

        hold_h_cfg = normalize_manual_override_hold_hours(
            config.get("manual_override_hold_hours", 2.0)
        )
        manual_holds = active_manual_holds(state, config=config, now=now)
        manual_hold_active = bool(manual_holds)
        if hold_h_cfg <= 0:
            manual_hold_detail = "aus (0 h)"
        elif manual_holds:
            bits = [
                f"{(h.get('label') or h.get('ip') or '?').split()[-1]}→{h.get('until_clock')}"
                for h in manual_holds[:4]
            ]
            manual_hold_detail = f"gesperrt {', '.join(bits)}"
        else:
            manual_hold_detail = f"Hold {hold_h_cfg:g} h nach Fremdschaltung"
    except Exception:
        hold_h_cfg = config.get("manual_override_hold_hours", 2.0)
    nodes.append(
        _node(
            id="manual_override",
            label="Fremdsteuerung-Hold",
            kind="gate",
            cadence="tick",
            route="idle" if manual_hold_active else arb_route,
            active=bool(actions) or manual_hold_active or export_guard or bat_lock or night,
            detail=manual_hold_detail[:110],
            live={
                "hold_hours": config.get("manual_override_hold_hours", 2.0),
                "active_count": len(manual_holds),
                "holds": manual_holds[:8],
            },
            info={
                "title": "Manuelle Fremdsteuerung",
                "purpose": (
                    "Nutzer schaltet Gerät manuell ein/aus → Algo lässt "
                    "dieses Gerät für X Stunden unangetastet (weder EIN noch AUS). "
                    "Countdown setzt sich bei neuer Fremdschaltung zurück."
                ),
                "formula": "Live ≠ last_action → manual_hold_until = now + hold_h",
                "sources": [
                    "config.manual_override_hold_hours",
                    "device_runtime.manual_hold_until",
                    "device_runtime.last_action",
                ],
                "module": "manual_override",
                "thresholds": "0 h = aus; Ausnahme Min-SoC-Notaus-Pause",
            },
            group="filter",
            x=XF,
            y=660,
        )
    )
    nodes.append(
        _node(
            id="hysterese",
            label="Hysterese",
            kind="gate",
            cadence="tick",
            route=arb_route,
            active=bool(actions) or export_guard or bat_lock or night,
            detail="min Gap ~5–15 min",
            live={},
            info={
                "title": "Hysterese",
                "purpose": "Mindestabstand zwischen Schaltungen.",
                "formula": "min_gap ≈ clamp(0.25×runtime, 300–900 s)",
                "sources": ["planner.apply_hysteresis"],
                "module": "planner",
                "thresholds": "300–900 s",
            },
            group="filter",
            x=XF,
            y=710,
        )
    )
    nodes.append(
        _node(
            id="plan_hold",
            label="Plan-Hold (Budget-Dip)",
            kind="gate",
            cadence="tick",
            route="on" if plan_hold_active else arb_route,
            active=bool(actions) or plan_hold_active or export_guard or bat_lock or night,
            detail=(
                "hält trotz Budget-Dip"
                if plan_hold_active
                else f"Hold {config.get('plan_budget_hold_hours', '—')} h"
            )[:110],
            live={"active": plan_hold_active},
            info={
                "title": "Plan-Hold bei Budget-Dip",
                "purpose": (
                    "Kurzfristige Verbrauchsspitzen sollen Geräte nicht aus dem "
                    "Laufzeitplan reißen, solange noch geplante Stunden übrig sind. "
                    "Wirkt in plan_load_actions und apply_device_runtime_guards."
                ),
                "formula": "plan_runtime_remaining_h ≥ plan_budget_hold_hours",
                "sources": ["priority_runtime_plan", "config.plan_budget_hold_hours"],
                "module": "load_scheduler.should_hold_through_budget_dip",
                "thresholds": "nahe Min-SoC → kein Hold",
            },
            group="filter",
            x=XF,
            y=760,
        )
    )
    nodes.append(
        _node(
            id="anom_resume_sperre",
            label="Anomalie-Resume-Sperre",
            kind="gate",
            cadence="15min",
            route="off" if anom_resume_block else arb_route,
            active=bool(actions) or anom_resume_block or export_guard or bat_lock or night,
            detail=(
                "keine neuen EIN nach Überverbrauch"
                if anom_resume_block
                else "frei"
            ),
            live={
                "blocked": anom_resume_block,
                "anomaly_cleared_at": state.get("anomaly_cleared_at"),
            },
            info={
                "title": "Anomalie-Resume-Sperre",
                "purpose": (
                    "Nach Überverbrauch-Anomalie: Einschalten erst nach Resume-Delay. "
                    "Unterverbrauch-Anomalie darf extra Last."
                ),
                "formula": "elapsed since anomaly_cleared_at < resume_delay",
                "sources": ["state.anomaly_*", "config.anomaly_resume_delay_minutes"],
                "module": "anomaly_detection.is_anomaly_resume_blocked",
                "thresholds": "Delay-Minuten aus Config",
            },
            group="filter",
            x=XF,
            y=860,
        )
    )
    nodes.append(
        _node(
            id="anker_live",
            label="Anker-Live-Check",
            kind="gate",
            cadence="tick",
            route="off" if anker_live_block else arb_route,
            active=bool(actions) or anker_live_block or export_guard or bat_lock or night,
            detail=(
                "Flex blockiert – Anker läuft nicht"
                if anker_live_block
                else "Anker bestätigt / kein Flex-Resume"
            )[:110],
            live={"blocked": anker_live_block},
            info={
                "title": "Anker-Live-Check vor Flex",
                "purpose": (
                    "Beim Anwenden: Flex/Spill nur EIN, wenn der Anker wirklich läuft "
                    "(frisch gelesen, nicht nur Plan)."
                ),
                "formula": "resume Flex nur wenn _anchor_confirmed_running()",
                "sources": ["service.run_tick apply-loop"],
                "module": "service",
                "thresholds": "Live-Status == running",
            },
            group="filter",
            x=XF,
            y=960,
        )
    )
    algo_route = "off" if not algo_enabled else arb_route
    nodes.append(
        _node(
            id="algo_aktiv",
            label="Steuerung aktiv",
            kind="guard",
            cadence="tick",
            route=algo_route,
            active=True,
            detail=(
                "deaktiviert – keine Schaltung"
                if not algo_enabled
                else ("Dry-Run – plant, schaltet nicht" if dry_run else "aktiv · schaltet")
            )[:110],
            live={"enabled": algo_enabled, "dry_run": dry_run},
            info={
                "title": "Steuerung Ein/Aus + Dry-Run",
                "purpose": (
                    "Master-Schalter. Aus → keine Pause/Resume. "
                    "Dry-Run plant Aktionen, sendet sie nicht an die Hardware."
                ),
                "formula": "config.enabled; config.dry_run / state.dry_run",
                "sources": ["control_algorithm_config.json"],
                "module": "service.run_tick + load_scheduler",
                "thresholds": "enabled=false → Budget-Plan bleibt, keine Befehle",
            },
            group="filter",
            x=XF,
            y=1060,
        )
    )

    try:
        from control_algorithm.anomaly_detection import _low_anomaly_energy_save_blocked
    except Exception:
        def _low_anomaly_energy_save_blocked(_state: Dict[str, Any]) -> bool:  # type: ignore
            return bool(_state.get("morning_floor_threatened"))

    safety_fast_pause = bool(
        bat_lock
        or safety
        or anom
        or any(
            a.get("action") == "pause" and (a.get("emergency") or a.get("force"))
            for a in actions
            if str(a.get("source") or "") in ("min_soc", "pv_safety", "anomaly")
            or a.get("emergency")
        )
    )
    safety_fast_resume = bool(
        (anom_low and not _low_anomaly_energy_save_blocked(state))
        or any(
            a.get("action") == "resume" and (a.get("emergency") or a.get("force"))
            for a in actions
            if str(a.get("source") or "") == "anomaly_low"
        )
    )
    safety_fast_active = safety_fast_pause or safety_fast_resume
    if safety_fast_pause:
        safety_fast_route = "off"
    elif safety_fast_resume:
        safety_fast_route = "on"
    else:
        safety_fast_route = "idle"
    nodes.append(
        _node(
            id="sicherheits_direkt",
            label="Sicherheits-Fast-Path",
            kind="guard",
            cadence="tick",
            route=safety_fast_route,
            active=safety_fast_active,
            detail=(
                "Not-Aus: umgeht Schiedsrichter/Min-Laufzeit/Hysterese"
                if safety_fast_pause
                else (
                    "Fast-Path Einschalten: umgeht Filterkette"
                    if safety_fast_resume
                    else "nur bei Batterie/PV-Sicherheit/Anomalie"
                )
            )[:110],
            live={
                "battery_lock": bat_lock,
                "pv_safety": safety,
                "anomaly_high": anom,
                "anomaly_low": anom_low,
            },
            info={
                "title": "Sicherheits-Fast-Path",
                "purpose": (
                    "Batterie-Schutz, PV-Sicherheit, Über- und Unterverbrauchs-Anomalie "
                    "schalten Miner direkt (force/emergency) – ohne Budget-Pipeline "
                    "und ohne Min-Laufzeit/Hysterese/Anti-Flatter."
                ),
                "formula": "force+emergency → forced_actions in run_tick (pause oder resume)",
                "sources": [
                    "check_min_soc_emergency",
                    "pv_safety_watchdog",
                    "anomaly_detection",
                ],
                "module": "service.run_tick",
                "thresholds": "Min-SoC · PV-Pace · Verbrauch ±X % vs. Hist.",
            },
            group="safety_fastpath",
            x=XF,
            y=1180,
        )
    )

    device_ids: List[str] = []
    for i, d in enumerate(devices):
        ip = str(d.get("ip") or "")
        if not ip:
            continue
        nid = f"dev_{ip.replace('.', '_')}"
        device_ids.append(nid)
        running = bool(d.get("running"))
        action = d.get("planned_action") or ""
        if ip in resume_ips:
            d_route = "on"
        elif ip in pause_ips:
            d_route = "off"
        elif running:
            d_route = "on"
        elif not running and (shed_active or bat_lock or night):
            d_route = "off"
        else:
            d_route = "idle"
        b_kwh = d.get("budget_kwh_24h")
        b_h = d.get("budget_hours_24h")
        budget_txt = ""
        if b_kwh is not None:
            try:
                budget_txt = f"{float(b_kwh):.1f} kWh"
                if b_h is not None:
                    budget_txt += f" / {float(b_h):.1f} h"
            except (TypeError, ValueError):
                budget_txt = ""
        nodes.append(
            _node(
                id=nid,
                label=str(d.get("name") or ip),
                kind="device",
                cadence="tick",
                route=d_route,
                active=d_route != "idle",
                detail=(
                    f"{d.get('status')} · {action} · {d.get('power_kw')} kW · "
                    f"Prio {d.get('priority_label')}"
                ),
                live={
                    "ip": ip,
                    "power_kw": d.get("power_kw"),
                    "running": running,
                    "planned_action": action,
                    "reason": d.get("reason"),
                    "budget_kwh_24h": b_kwh,
                    "budget_hours_24h": b_h,
                    "budget_label": budget_txt or None,
                },
                info={
                    "title": f"Gerät {d.get('name') or ip}",
                    "purpose": "Endknoten EIN/AUS.",
                    "formula": "resume/pause nach Guards",
                    "sources": ["planned_actions"],
                    "module": "plan_load_actions",
                    "thresholds": d.get("reason") or "—",
                },
                group="devices",
                x=XDEV,
                y=40 + i * 100,
            )
        )

    # ---- Edges: fan-in hubs (Farbe = Quell-Kästchen, kein flächiges Grün) ----
    add(
        "wetter_oms",
        "hub_wetter",
        color=oms_route,
        active=oms_route != "idle",
    )
    add(
        "muster_wetter",
        "hub_wetter",
        color=muster_w_route,
        active=muster_w_route != "idle",
    )
    add(
        "pv_sicherheit",
        "hub_wetter",
        color="off" if safety else "idle",
        active=safety,
    )
    add(
        "wetter_massstab",
        "hub_wetter",
        color=oms_route,
        active=oms_route != "idle",
    )
    add(
        "stundenfaktoren",
        "hub_wetter",
        color="on" if hour_slots_active else "idle",
        active=hour_slots_active,
    )
    add(
        "wetter_massstab",
        "wetter_oms",
        color=oms_route,
        active=oms_route != "idle",
        style="dashed",
    )
    add(
        "stundenfaktoren",
        "wetter_oms",
        color="on" if hour_slots_active else "idle",
        active=hour_slots_active,
        style="dashed",
    )

    add(
        "live_soc",
        "hub_live",
        color="off" if bat_lock else "idle",
        active=bat_lock,
    )
    add("live_pv", "hub_live", color="idle", active=False)
    add(
        "netz_export",
        "hub_live",
        color=netz_route,
        active=netz_route != "idle",
    )
    add(
        "live_verbrauch",
        "hub_live",
        color="off" if anom else ("on" if anom_low else "idle"),
        active=anom or anom_low,
    )
    add(
        "live_hydro",
        "hub_live",
        color="on" if (hydro_kw is not None and hydro_kw >= 0.05) else "idle",
        active=hydro_kw is not None and hydro_kw >= 0.05,
    )
    add(
        "live_batterie",
        "hub_live",
        color=bat_route,
        active=bat_route != "idle",
    )

    # Verbrauch: Paar-Reihen links→rechts→Hub (keine Kreuzung durch Nachbarknoten)
    add("verbrauch_hist", "verbrauch_muster", color="idle", active=False)
    add("verbrauch_muster", "hub_verbrauch", color="idle", active=False)
    add(
        "verbrauch_anomalie_hoch",
        "performance_ratio",
        color="off" if anom else "idle",
        active=anom,
    )
    add("performance_ratio", "hub_verbrauch", color="idle", active=False)
    add(
        "verbrauch_anomalie_tief",
        "referenz_anker",
        color="on" if anom_low else "idle",
        active=anom_low,
    )
    add(
        "referenz_anker",
        "hub_verbrauch",
        color="on" if ref_active else "idle",
        active=ref_active,
    )
    add("abend_kalibrierung", "hub_verbrauch", color="idle", active=False)
    add(
        "soc_lernen",
        "hub_verbrauch",
        color="off" if soc_learn_active else "idle",
        active=soc_learn_active,
        style="dashed",
    )
    add(
        "abend_kalibrierung",
        "soc_lernen",
        color="idle",
        active=False,
        style="dashed",
    )

    add(
        "hub_wetter",
        "energiebilanz",
        color=weather_hub_route,
        active=weather_hub_route != "idle",
    )
    add(
        "hub_live",
        "energiebilanz",
        color=live_hub_route,
        active=live_hub_route != "idle",
    )
    add(
        "hub_verbrauch",
        "energiebilanz",
        color=verbrauch_hub_route,
        active=verbrauch_hub_route != "idle",
    )

    add("energiebilanz", "risk_faktor", color=feed, active=True)
    add(
        "energiebilanz",
        "verbrauch_risiko",
        color="off" if cons_risk_active else "idle",
        active=cons_risk_active,
    )
    add(
        "energiebilanz",
        "soc_ziele",
        color="off" if (cp_fail or morning_threat) else feed,
        active=True,
    )
    # Soll-Kurve: run_tick berechnet curve_plan vor maybe_run_hourly_balance (parallel zu Bilanz).
    add(
        "hub_live",
        "linienalgorithmus",
        label="Tick zuerst",
        color=linien_route if curve.get("ok") else "idle",
        active=bool(curve.get("ok")),
    )
    add(
        "energiebilanz",
        "autopilot_survival",
        color="off" if not tomorrow_ok else "idle",
        active=not tomorrow_ok,
    )
    # Hauptpfad Mitte frei; Lernfaktoren am Rand mit strichlierter Anbindung
    add(
        "linienalgorithmus",
        "batterie_schutz",
        color=linien_route if curve.get("ok") else "idle",
        active=bool(curve.get("ok")),
    )
    add(
        "linienalgorithmus",
        "steering_learning",
        color=linien_route if curve.get("ok") else "idle",
        active=bool(curve.get("ok")),
        style="dashed",
    )
    add("steering_learning", "risk_neuron", color="idle", active=False, style="dashed")
    add(
        "steering_learning",
        "day_night_neuron",
        color="on" if dn_active else "idle",
        active=True,
        style="dashed",
    )
    add(
        "day_night_neuron",
        "near_term",
        color="on" if dn_active else "idle",
        active=True,
        style="dashed",
    )
    add(
        "day_night_neuron",
        "prio_laufzeit",
        color="on" if dn_active else "idle",
        active=True,
        style="dashed",
    )
    add(
        "steering_learning",
        "speicher_band_neuron",
        color="on" if band_active else "idle",
        active=band_active,
        style="dashed",
    )
    add(
        "steering_learning",
        "lehrneuron_prio_balance",
        color="on" if lehr_lit else "idle",
        active=True,
        style="dashed",
    )
    add(
        "steering_learning",
        "lehrneuron_soc_prio",
        color="on" if spn_lit else "idle",
        active=spn_enabled,
        style="dashed",
    )
    # Lehrneuron → rechte Steuerungskette (Export / Prio / Schiedsrichter)
    add(
        "lehrneuron_prio_balance",
        "export_vermeidung",
        color="on" if lehr_risk or lehr_spill > 0.1 else "idle",
        active=True,
        style="dashed",
    )
    add(
        "lehrneuron_prio_balance",
        "prio_laufzeit",
        color="on" if lehr_anchor > 0.1 else "idle",
        active=True,
        style="dashed",
    )
    add(
        "lehrneuron_prio_balance",
        "near_term",
        color="on" if lehr_lit else "idle",
        active=True,
        style="dashed",
    )
    add(
        "lehrneuron_prio_balance",
        "schiedsrichter",
        color="on" if lehr_risk or lehr_spill > 0.1 else "idle",
        active=True,
        style="dashed",
    )
    add(
        "lehrneuron_soc_prio",
        "prio_laufzeit",
        color="on" if spn_lit else "idle",
        active=spn_enabled,
        style="dashed",
    )
    add(
        "lehrneuron_soc_prio",
        "near_term",
        color="on" if spn_lit else "idle",
        active=spn_enabled,
        style="dashed",
    )
    # Neuron wirkt auf der Planebene (deckelt das Tagesziel), nicht am Schalter
    add(
        "lehrneuron_soc_prio",
        "tages_ueberschuss",
        label="deckelt Ziel",
        color="on" if spn_lit and not spn_shadow else "idle",
        active=spn_enabled,
        style="dashed",
    )
    add(
        "lehrneuron_soc_prio",
        "linienalgorithmus",
        color="on" if spn_lit else "idle",
        active=spn_enabled,
        style="dashed",
    )
    add(
        "speicher_band_neuron",
        "max_soc",
        color="on" if band_active else "idle",
        active=band_active,
        style="dashed",
    )
    add(
        "speicher_band_neuron",
        "batterie_schutz",
        color="on" if band_active else "idle",
        active=band_active,
        style="dashed",
    )
    add(
        "speicher_band_neuron",
        "wochenplaner",
        color="on" if band_active else "idle",
        active=band_active,
        style="dashed",
    )
    add("risk_neuron", "dazugelernt", color="idle", active=False, style="dashed")
    add("steering_learning", "dazugelernt", color="idle", active=False, style="dashed")
    add("dazugelernt", "batterie_schutz", color="idle", active=False, style="dashed")
    # Risiko-Neuron: 2 Wirk-Anschlüsse (wie Wetter-OMS → wetter/muster)
    add(
        "risk_neuron",
        "switch_limit",
        color="on" if risk_active else "idle",
        active=risk_active,
        style="dashed",
    )
    add(
        "risk_neuron",
        "switch_commitment",
        color="on" if risk_active else "idle",
        active=risk_active,
        style="dashed",
    )
    add(
        "lernfaktor_wetter_oms",
        "wetter_oms",
        color="on" if oms_learn_active else "idle",
        active=oms_learn_active,
        style="dashed",
    )
    add(
        "lernfaktor_wetter_oms",
        "muster_wetter",
        color="on" if oms_learn_active else "idle",
        active=oms_learn_active,
        style="dashed",
    )
    add(
        "abend_kalibrierung",
        "lernfaktor_wetter_oms",
        color="idle",
        active=False,
        style="dashed",
    )
    add("risk_faktor", "batterie_schutz", color="idle", active=False)
    add(
        "verbrauch_risiko",
        "batterie_schutz",
        color="off" if cons_risk_active else "idle",
        active=cons_risk_active,
    )
    add(
        "soc_ziele",
        "batterie_schutz",
        color="off" if (cp_fail or morning_threat) else "idle",
        active=cp_fail or morning_threat,
    )
    add(
        "autopilot_survival",
        "batterie_schutz",
        color="off" if not tomorrow_ok else "idle",
        active=not tomorrow_ok,
    )
    add(
        "batterie_schutz",
        "ueberschuss",
        color="off" if bat_lock else surplus_route,
        active=True,
    )
    add(
        "batterie_schutz",
        "max_soc",
        color="on" if (max_soc_over or fill_urgent) else "idle",
        active=max_soc_over or fill_urgent or max_soc_near,
    )
    add(
        "max_soc",
        "stufe1_halteband",
        color=hold_route,
        active=hold_on or hold_released,
    )
    add(
        "stufe1_halteband",
        "ueberschuss_session",
        label="aus bis Max" if hold_released else "ein",
        color=hold_route,
        active=hold_on or hold_released,
        style="dashed",
    )
    add(
        "max_soc",
        "ueberschuss",
        color="on" if (max_soc_over or fill_urgent) else "idle",
        active=max_soc_over or fill_urgent,
    )
    add(
        "max_soc",
        "fuell_dringlichkeit",
        color="on" if fill_urgent else "idle",
        active=fill_urgent,
    )

    # Planebene: Tages-Überschuss speist die Ja/Nein-Entscheidung und das Session-Ziel
    add(
        "energiebilanz",
        "tages_ueberschuss",
        label="Rest heute",
        color="on" if today_ok else "idle",
        active=today_ok,
    )
    add(
        "tages_ueberschuss",
        "ueberschuss",
        label=f"{today_kwh} kWh",
        color="on" if (today_ok and today_kwh > 0.3) else "idle",
        active=today_ok,
    )
    add(
        "ueberschuss",
        "ueberschuss_session",
        label="Ziel",
        color="on" if session_active and not session_suspended_by else "idle",
        active=session_active,
    )
    add(
        "ueberschuss_session",
        "schiedsrichter",
        label="hält Plan",
        color="on" if session_active and not session_suspended_by else "idle",
        active=session_active,
    )
    # Risiko bremst die Session, statt einen Gegenplan zu fahren
    add(
        "pv_sicherheit",
        "ueberschuss_session",
        label="pausiert",
        color="off" if (safety and safety_enabled and not safety_suspended) else "idle",
        active=bool(safety and safety_enabled),
        style="dashed",
    )
    add(
        "verbrauch_anomalie_hoch",
        "ueberschuss_session",
        label="pausiert",
        color="off" if anom else "idle",
        active=anom,
        style="dashed",
    )
    add(
        "nacht_sperre",
        "ueberschuss_session",
        label="beendet",
        color="off" if night else "idle",
        active=night,
        style="dashed",
    )
    add(
        "batterie_schutz",
        "ueberschuss_session",
        label="beendet",
        color="off" if bat_lock else "idle",
        active=bat_lock,
        style="dashed",
    )

    add(
        "ueberschuss",
        "export_vermeidung",
        label="Ja",
        color="on" if yes_active else "idle",
        active=yes_active,
    )
    add(
        "ueberschuss",
        "wochenplaner",
        label="Nein",
        color="off" if no_active else "idle",
        active=no_active,
    )
    add(
        "wochenplaner",
        "schlechtwetter_streak",
        label="7-Tage",
        color=week_route if week_active else "idle",
        active=week_active,
    )
    add(
        "schlechtwetter_streak",
        first_horiz,
        label="Tag 1",
        color=week_route if week_active else "idle",
        active=week_active,
    )
    add("horizon_sim", first_horiz, color="idle", active=week_active)
    add("dynam_soc_ziele", first_horiz, color="idle", active=week_active)
    add(
        "wochenplaner",
        first_horiz,
        label="Tag für Tag",
        color=week_route if week_active else "idle",
        active=week_active,
    )
    add(
        "wochenplaner",
        "nacht_sperre",
        label="Nacht",
        color="off" if night else "idle",
        active=night,
    )
    add(
        "wochenplaner",
        "last_reduzieren",
        label="knapp",
        color="off" if (no_active and not tomorrow_ok) else "idle",
        active=no_active and not tomorrow_ok,
    )
    add(
        "soc_ziele",
        "morgen_floor",
        color="off" if morning_threat else "idle",
        active=morning_threat,
    )
    add(
        "morgen_floor",
        "last_reduzieren",
        color="off" if morning_shed else "idle",
        active=morning_shed,
    )
    add(
        "morgen_floor",
        "partei_1",
        color="on" if (daytime_pv_hold and morning_threat) else "idle",
        active=daytime_pv_hold and morning_threat,
    )
    add(
        "fuell_dringlichkeit",
        "morgen_floor",
        label="kein Shed",
        color="on" if (fill_urgent and morning_threat) else "idle",
        active=fill_urgent and morning_threat,
    )

    add("export_vermeidung", "near_term", color=on_path if yes_active else "idle", active=yes_active)
    add("near_term", "prio_laufzeit", color=on_path if yes_active else "idle", active=yes_active)
    add(
        "ueberschuss",
        "fuell_dringlichkeit",
        label="Urgency",
        color="on" if fill_urgent else "idle",
        active=fill_urgent,
    )
    add(
        "ueberschuss",
        "wetter_durchlauf",
        color="on" if run_through_active else "idle",
        active=run_through_active,
    )
    add(
        "fuell_dringlichkeit",
        "partei_1",
        color="on" if fill_urgent else "idle",
        active=fill_urgent,
    )
    add(
        "wetter_durchlauf",
        "partei_1",
        color="on" if run_through_active else "idle",
        active=run_through_active,
    )
    add(
        "fuell_dringlichkeit",
        "fuellplan",
        color="on" if fill_urgent else "idle",
        active=fill_urgent,
    )
    add(
        "prio_laufzeit",
        "fuellplan",
        color=on_path if (yes_active or hold_active) else "idle",
        active=yes_active or hold_active,
    )
    add(
        "prio_laufzeit",
        "spaetstart",
        color="idle" if late_active else on_path,
        active=yes_active or late_active,
    )
    add(
        "fuellplan",
        "anker_streckung",
        color="off" if stretch_active else on_path,
        active=yes_active or stretch_active or fill_now,
    )
    add("spaetstart", "laufreserve", color="idle", active=late_active)
    add(
        "laufreserve",
        "morgen_drain",
        color="off" if drain_blocks else "idle",
        active=drain_blocks,
    )
    add(
        "morgen_drain",
        "drain_neuron",
        color="idle",
        active=drain_ok,
        style="dashed",
        label="Lernpfad",
    )
    add(
        "drain_neuron",
        "laufreserve",
        color="idle",
        active=drain_ok,
        style="dashed",
        label="m_risk",
    )
    add(
        "morgen_drain",
        "anker_streckung",
        color="off" if drain_blocks else "idle",
        active=drain_ok,
    )
    add(
        "anker_streckung",
        "partei_1",
        color=("off" if stretch_active else on_path)
        if (yes_active or hold_active or stretch_active)
        else "idle",
        active=yes_active or hold_active or stretch_active,
    )
    add(
        "partei_1",
        "prio_invariante",
        color=on_path if partei1_active else "idle",
        active=partei1_active,
    )

    prod_suffixes = ("pv", "wetter", "modus", "verbrauch", "hydro", "soc", "slots")
    for i, row in enumerate(horizon_rows):
        offset = int(row.get("offset") if row.get("offset") is not None else i)
        day_num = offset + 1
        nid = str(row["id"])
        ja_id = f"horiz_d{day_num}_ja"
        nein_id = f"horiz_d{day_num}_nein"
        nxt = f"horiz_d{day_num + 1}" if day_num < len(horizon_rows) else None
        day_ok = bool(row.get("ok"))
        for j, suffix in enumerate(prod_suffixes):
            if j > 0:
                prev = prod_suffixes[j - 1]
                add(
                    f"horiz_d{day_num}_{prev}",
                    f"horiz_d{day_num}_{suffix}",
                    color=week_route if week_active else "idle",
                    active=week_active,
                )
        add(
            f"horiz_d{day_num}_{prod_suffixes[-1]}",
            nid,
            color=week_route if week_active else "idle",
            active=week_active,
        )
        add(
            nid,
            ja_id,
            label="Ja",
            color="on" if day_ok and no_active else "idle",
            active=day_ok and no_active,
        )
        add(
            nid,
            nein_id,
            label="Nein",
            color="off" if not day_ok and no_active else "idle",
            active=not day_ok and no_active,
        )
        if nxt:
            add(
                ja_id,
                nxt,
                label="weiter",
                color="on" if day_ok and no_active else "idle",
                active=day_ok and no_active,
            )
            add(
                nein_id,
                nxt,
                label="weiter",
                color="idle",
                active=no_active,
            )
        if not day_ok:
            add(
                nein_id,
                "last_reduzieren",
                label="Nein",
                color="off" if no_active else "idle",
                active=no_active,
            )
        if nxt is None:
            add(
                ja_id,
                "prio_laufzeit",
                label="Ja",
                color="on" if hold_active else "idle",
                active=hold_active,
            )
            add(
                nein_id,
                "nacht_sperre",
                label="Nein",
                color="off" if (no_active and not tomorrow_ok) else "idle",
                active=no_active and not tomorrow_ok,
            )

    add(
        "nacht_sperre",
        "last_reduzieren",
        color="off" if night else "idle",
        active=night,
    )
    add(
        "last_reduzieren",
        "partei_2",
        color="off" if shed_active else "idle",
        active=shed_active,
    )
    add(
        "partei_2",
        "prio_invariante",
        color="off" if partei2_active else "idle",
        active=partei2_active,
    )

    if safety:
        add("pv_sicherheit", "last_reduzieren", color="off", active=True)
    if anom:
        add("verbrauch_anomalie_hoch", "last_reduzieren", color="off", active=True)
    if anom_resume_block:
        add("verbrauch_anomalie_hoch", "anom_resume_sperre", color="off", active=True)

    if bat_lock:
        add(
            "batterie_schutz",
            "sicherheits_direkt",
            color="off",
            active=True,
            style="dashed",
        )
    if safety:
        add(
            "pv_sicherheit",
            "sicherheits_direkt",
            color="off",
            active=True,
            style="dashed",
        )
    if anom:
        add(
            "verbrauch_anomalie_hoch",
            "sicherheits_direkt",
            color="off",
            active=True,
            style="dashed",
        )
    if anom_low and not _low_anomaly_energy_save_blocked(state):
        add(
            "verbrauch_anomalie_tief",
            "sicherheits_direkt",
            color="on",
            active=True,
            style="dashed",
        )
    add(
        "sicherheits_direkt",
        "algo_aktiv",
        color=safety_fast_route,
        active=safety_fast_active,
        style="dashed",
    )

    # Schalt-Filter: Reihenfolge wie run_tick (… → commitment → manual_override → hysterese → apply).
    # Die Session sitzt als Stufe plan_session zwischen Notbremse und Reserve/Floor.
    add("prio_invariante", "schiedsrichter", color=arb_route, active=True)
    add("schiedsrichter", "plan_hold", color=arb_route, active=True)
    add("plan_hold", "anom_resume_sperre", color=arb_route, active=True)
    add("anom_resume_sperre", "min_runtime", color=arb_route, active=True)
    add("min_runtime", "min_off", color=arb_route, active=True)
    add("min_off", "switch_commitment", color=arb_route, active=True)
    add("switch_commitment", "switch_limit", color=arb_route, active=True)
    add("switch_limit", "manual_override", color=arb_route, active=True)
    add("manual_override", "hysterese", color=arb_route, active=True)
    add("hysterese", "anker_live", color=arb_route, active=True)
    add("anker_live", "algo_aktiv", color=arb_route, active=True)
    add(
        "fuell_dringlichkeit",
        "switch_commitment",
        color="on" if fill_urgent else "idle",
        active=fill_urgent,
    )

    for nid in device_ids:
        n = next((x for x in nodes if x["id"] == nid), None)
        if not n:
            continue
        add("algo_aktiv", nid, color=n.get("route") or "idle", active=bool(n.get("active")))

    _apply_edge_flow(nodes, edges)
    layout_customized = _apply_layout_overrides(nodes, state)
    layout_blob = state.get("logic_gate_layout") or {}
    edge_waypoints = {}
    raw_wp = layout_blob.get("edge_waypoints") or {}
    if isinstance(raw_wp, dict):
        for ek, pos in raw_wp.items():
            if not isinstance(pos, dict):
                continue
            try:
                edge_waypoints[str(ek)] = {
                    "x": float(pos["x"]),
                    "y": float(pos["y"]),
                }
            except (KeyError, TypeError, ValueError):
                continue
    active_edges = [
        e for e in edges if e.get("active") and e.get("color") in ("on", "off")
    ]
    direction_label = {
        "on": "Mehr Last",
        "off": "Weniger Last",
        "idle": "Neutral / wartet",
    }.get(global_route, "Live")

    max_y = max((float(n.get("y") or 0) + 100 for n in nodes), default=900)
    max_x = max((float(n.get("x") or 0) + 220 for n in nodes), default=2000)

    cadence_meta = _enrich_cadence_meta(config, state, now=now)

    def _leg(cadence: str, base: str, style: str, color: str) -> Dict[str, Any]:
        m = cadence_meta.get(cadence) or {}
        nxt = m.get("band_next") or m.get("short_next") or "—"
        return {
            "cadence": cadence,
            "label": f"{base} · {nxt}",
            "style": style,
            "color": color,
            "next_at": m.get("next_at"),
            "next_clock": m.get("next_clock"),
            "next_label": m.get("next_label"),
        }

    return {
        "title": "Logikgattersteuerungsalgorithmus Autopilot",
        "layout_rev": LOGIC_GATE_LAYOUT_REV,
        "layout_customized": layout_customized,
        "layout_saved_at": (state.get("logic_gate_layout") or {}).get("updated_at"),
        "edge_waypoints": edge_waypoints,
        "updated_at": now.isoformat(),
        "direction": global_route,
        "direction_label": direction_label,
        "headline": (
            f"{direction_label} · Budget {budget_kw if budget_kw is not None else '—'} kW · "
            f"SoC-Lücke {score.get('soc_gap_pct') if score.get('soc_gap_pct') is not None else '—'} %-Pkt · "
            f"Pace {pace if pace is not None else '—'} %"
        ),
        "score": score,
        "cadence_meta": cadence_meta,
        "legend": [
            _leg("3h", "alle 3 Stunden", "dashed", "#ffb74d"),
            _leg("hourly", "stündlich", "solid", "#64b5f6"),
            _leg("tick", "jeder Tick", "solid", "#81c784"),
            _leg("15min", "Anomalie ~15 min", "solid", "#ce93d8"),
            _leg("daily", "täglich abends", "dashed", "#90a4ae"),
            {"route": "on", "label": "Pfad EIN / mehr Last", "color": "#66bb6a"},
            {"route": "off", "label": "Pfad AUS / weniger Last", "color": "#ef5350"},
        ],
        "nodes": nodes,
        "edges": edges,
        "active_edges": active_edges,
        "curve_plan": curve if isinstance(curve, dict) else {},
        "viewbox": {"w": max(2100, int(max_x)), "h": max(2400, int(max_y))},
        "summary": {
            "has_surplus": has_surplus,
            "tomorrow_ok": tomorrow_ok,
            "battery_lock": bat_lock,
            "yield_factor": yield_factor,
            "today_miss_ticks": miss_ticks,
            "today_adjustments": adj_n,
            "node_count": len(nodes),
        },
    }
