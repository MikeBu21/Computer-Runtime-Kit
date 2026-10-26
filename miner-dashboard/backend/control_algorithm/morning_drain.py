# -*- coding: utf-8 -*-
"""
Morgen-Drain: Laufreserve g als einzige Nachtschranke.

Abendstopp und Morgen-Frühstart sind dieselbe Ungleichung:

    g(t, P) = (s(t) − s_floor)/100 · C
              − ∫[t → t_be] (B + P − H − G) dτ
              − m_risk(t)

Geräte dürfen aus dem Speicher laufen, solange g ≥ 0. Abends ist das Fenster
bis zum nächsten Break-even lang, g also tief negativ → aus. Nachts wächst g
mit der Geräteleistung, der Nulldurchgang ist der Frühstart.

Alles in kWh aus storage_kwh; keine festen Uhrzeiten, keine absoluten
kWh-Schwellen. Break-even ist die Nullstelle von G + H − (B + P), abends wie
morgens — nicht last_pv_hour_for_date (feste Schwelle + Stundenklammer).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

SLOT_MINUTES = 15
BREAK_EVEN_HORIZON_HOURS = 36.0
REACTION_HOURS = 0.25
PARAM_KEYS = (
    "drain_base_load_bias",
    "drain_night_risk_frac",
    "drain_floor_margin_kwh_pct",
    "drain_pv_trust",
    "drain_pv_onset_bias_min",
    "drain_device_kw_bias",
)


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _params(state: Dict[str, Any]) -> Dict[str, float]:
    from control_algorithm.steering_learning import get_steering_params

    return get_steering_params(state)


# ---------------------------------------------------------------- Zeitreihen


def _pv_kwh(state: Dict[str, Any], start: datetime, end: datetime) -> float:
    if end <= start:
        return 0.0
    try:
        from control_algorithm.energy_balance import _pv_forecast_kwh

        return max(0.0, _f(_pv_forecast_kwh(state, start, end)))
    except Exception:
        return 0.0


def _base_kwh(base_dir: str, start: datetime, end: datetime) -> float:
    if end <= start:
        return 0.0
    try:
        from control_algorithm.consumption_history import expected_base_consumption_kwh

        return max(0.0, _f(expected_base_consumption_kwh(base_dir, start, end)))
    except Exception:
        return 0.0


def _hydro_kwh(base_dir: str, start: datetime, end: datetime) -> float:
    if end <= start:
        return 0.0
    try:
        from control_algorithm.consumption_history import expected_hydro_production_kwh

        return max(0.0, _f(expected_hydro_production_kwh(base_dir, start, end)))
    except Exception:
        return 0.0


def _peak_base_kw(base_dir: str, now: datetime) -> float:
    """Haushaltsspitze aus dem Wochentagsprofil – Sockel für m_base."""
    try:
        from control_algorithm.consumption_history import (
            get_weekday_profile,
            load_history,
        )

        history = load_history(base_dir)
        prof = get_weekday_profile(history, now.weekday(), base_dir)
        hourly = prof.get("hourly_base_kwh_avg") or []
        peak = max((_f(x) for x in hourly), default=0.0)
        if peak > 0:
            return peak
    except Exception:
        pass
    return 1.0


def _net_draw_kwh(
    state: Dict[str, Any],
    base_dir: str,
    start: datetime,
    end: datetime,
    *,
    device_kw: float,
    pv_scale: float = 1.0,
    base_bias: float = 1.0,
) -> float:
    """Energie, die der Speicher im Intervall liefern muss (negativ = Laden)."""
    if end <= start:
        return 0.0
    hours = (end - start).total_seconds() / 3600.0
    load = _base_kwh(base_dir, start, end) * base_bias + device_kw * hours
    supply = _pv_kwh(state, start, end) * pv_scale + _hydro_kwh(base_dir, start, end)
    return load - supply


def _slot_bounds(now: datetime, horizon_hours: float) -> List[Tuple[datetime, datetime]]:
    step = timedelta(minutes=SLOT_MINUTES)
    out: List[Tuple[datetime, datetime]] = []
    t = now.replace(second=0, microsecond=0)
    limit = now + timedelta(hours=horizon_hours)
    while t < limit:
        out.append((t, t + step))
        t = t + step
    return out


# ---------------------------------------------------------------- Break-even


def _covered(
    state: Dict[str, Any],
    base_dir: str,
    start: datetime,
    end: datetime,
    *,
    device_kw: float,
    pv_scale: float,
    base_bias: float,
) -> bool:
    """PV + Wasserkraft decken Grundlast + Geräte in diesem Slot."""
    return (
        _net_draw_kwh(
            state,
            base_dir,
            start,
            end,
            device_kw=device_kw,
            pv_scale=pv_scale,
            base_bias=base_bias,
        )
        <= 0.0
    )


def next_break_even(
    state: Dict[str, Any],
    base_dir: str,
    *,
    device_kw: float,
    now: datetime,
    pv_scale: float = 1.0,
    base_bias: float = 1.0,
    horizon_hours: float = BREAK_EVEN_HORIZON_HOURS,
) -> Optional[datetime]:
    """
    Erster Zeitpunkt ab now, an dem PV + Wasserkraft die Last tragen.

    Das ist t_be. Gibt es keine Nullstelle im Horizont (Schlechtwetter oder
    device_kw über der PV-Spitze), ist das Ergebnis None – dann kein Drain.
    """
    for start, end in _slot_bounds(now, horizon_hours):
        if _covered(
            state,
            base_dir,
            start,
            end,
            device_kw=device_kw,
            pv_scale=pv_scale,
            base_bias=base_bias,
        ):
            return start
    return None


def coverage_end(
    state: Dict[str, Any],
    base_dir: str,
    *,
    device_kw: float,
    now: datetime,
    pv_scale: float = 1.0,
    base_bias: float = 1.0,
    horizon_hours: float = BREAK_EVEN_HORIZON_HOURS,
) -> Optional[datetime]:
    """
    Letzter Zeitpunkt der laufenden Deckungsphase – der Abend-Break-even.

    None, wenn jetzt gar nicht gedeckt ist.
    """
    last: Optional[datetime] = None
    for start, end in _slot_bounds(now, horizon_hours):
        if _covered(
            state,
            base_dir,
            start,
            end,
            device_kw=device_kw,
            pv_scale=pv_scale,
            base_bias=base_bias,
        ):
            last = end
        elif last is not None:
            return last
        elif start > now:
            return None
    return last


# ---------------------------------------------------------------- Laufreserve


def floor_soc_percent(config: Dict[str, Any]) -> float:
    min_soc = _f(config.get("min_soc_percent"), 35.0)
    morning = config.get("morning_soc_percent")
    morning_f = _f(morning, min_soc) if morning is not None else min_soc
    return max(min_soc, morning_f)


def m_base_kwh(
    config: Dict[str, Any],
    state: Dict[str, Any],
    base_dir: str,
    now: datetime,
) -> float:
    """
    Konstanter Sockel des Sicherheitspuffers.

    Rein prozentual wäre er bei winzigen Speichern bedeutungslos (2 % von
    5 kWh sind eine Minute Ankerlaufzeit), deshalb zusätzlich eine
    Haushaltsspitze mal Reaktionszeit.
    """
    storage = _f(config.get("storage_kwh"))
    pct = _params(state).get("drain_floor_margin_kwh_pct", 2.0)
    relative = pct / 100.0 * storage
    absolute = _peak_base_kw(base_dir, now) * REACTION_HOURS
    return max(relative, absolute)


def deadband_kwh(config: Dict[str, Any], device_kw: float) -> float:
    """Totband gegen Flattern am Nulldurchgang (SoC-Auflösung ~1 %)."""
    storage = _f(config.get("storage_kwh"))
    return max(0.01 * storage, 0.05 * max(0.0, device_kw))


def run_reserve_kwh(
    config: Dict[str, Any],
    state: Dict[str, Any],
    base_dir: str,
    *,
    soc_percent: float,
    device_kw: float,
    t_be: datetime,
    now: datetime,
    pv_scale: float = 1.0,
) -> Dict[str, Any]:
    """
    g – wie viele kWh über dem Floor übrig bleiben, wenn die Geräte ab jetzt
    bis zum Break-even durchlaufen.
    """
    storage = _f(config.get("storage_kwh"))
    s_floor = floor_soc_percent(config)
    params = _params(state)
    base_bias = params.get("drain_base_load_bias", 1.0)
    risk_frac = params.get("drain_night_risk_frac", 0.25)

    available = (soc_percent - s_floor) / 100.0 * storage
    need = max(
        0.0,
        _net_draw_kwh(
            state,
            base_dir,
            now,
            t_be,
            device_kw=device_kw,
            pv_scale=pv_scale,
            base_bias=base_bias,
        ),
    )
    household = max(
        0.0,
        _base_kwh(base_dir, now, t_be) * base_bias - _hydro_kwh(base_dir, now, t_be),
    )
    m_risk = max(0.0, m_base_kwh(config, state, base_dir, now) + risk_frac * household)

    return {
        "g_kwh": round(available - need - m_risk, 3),
        "available_kwh": round(available, 3),
        "need_kwh": round(need, 3),
        "household_kwh": round(household, 3),
        "m_risk_kwh": round(m_risk, 3),
        "floor_soc_percent": round(s_floor, 1),
        "hours_to_break_even": round((t_be - now).total_seconds() / 3600.0, 2),
    }


# ---------------------------------------------------------------- Gerätemenge


def _anchor_first(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    from control_algorithm.planner import enabled_devices, sort_for_resume

    return list(sort_for_resume(enabled_devices(config)))


def _device_kw(device: Dict[str, Any], bias: float) -> float:
    kw = _f(device.get("measured_power_kw")) or _f(device.get("power_kw"))
    return max(0.0, kw * bias)


def reference_break_even(
    config: Dict[str, Any],
    state: Dict[str, Any],
    base_dir: str,
    *,
    now: datetime,
    pv_scale: float = 1.0,
) -> Optional[datetime]:
    """
    Gemeinsamer Horizont für alle g-Auswertungen.

    Bevorzugt der Break-even der vollen Gerätemenge. Trägt PV die nie (große
    Last, kleine Anlage), gilt der des höchstprioren Geräts – der Moment, ab
    dem PV wenigstens die wichtigste Last übernimmt. Ohne beides gibt es kein
    Fenster und damit kein Gate.
    """
    ranked = _anchor_first(config)
    if not ranked:
        return None
    bias = _params(state).get("drain_device_kw_bias", 1.0)
    candidates = [sum(_device_kw(d, bias) for d in ranked)]
    candidates.append(_device_kw(ranked[0], bias))
    candidates.append(min(_device_kw(d, bias) for d in ranked))
    for kw in candidates:
        if kw <= 0:
            continue
        t_be = next_break_even(
            state, base_dir, device_kw=kw, now=now, pv_scale=pv_scale
        )
        if t_be is not None and t_be > now:
            return t_be
    return None


def feasible_device_set(
    config: Dict[str, Any],
    state: Dict[str, Any],
    base_dir: str,
    *,
    soc_percent: float,
    now: datetime,
    t_be: datetime,
    pv_scale: float = 1.0,
) -> Dict[str, Any]:
    """
    Größte Gerätemenge nach Priorität, für die g ≥ Totband gilt.

    Alle Kandidaten werden gegen denselben Horizont gerechnet, damit g streng
    monoton in der Geräteleistung ist – sonst wäre der Greedy nicht eindeutig.
    """
    ranked = _anchor_first(config)
    bias = _params(state).get("drain_device_kw_bias", 1.0)
    for cut in range(len(ranked), 0, -1):
        subset = ranked[:cut]
        total_kw = sum(_device_kw(d, bias) for d in subset)
        if total_kw <= 0:
            continue
        reserve = run_reserve_kwh(
            config,
            state,
            base_dir,
            soc_percent=soc_percent,
            device_kw=total_kw,
            t_be=t_be,
            now=now,
            pv_scale=pv_scale,
        )
        if reserve["g_kwh"] >= deadband_kwh(config, total_kw):
            return {
                "ips": [str(d.get("ip") or d.get("entity_id") or "") for d in subset],
                "device_kw": round(total_kw, 3),
                "break_even_at": t_be,
                "reserve": reserve,
            }
    return {"ips": [], "device_kw": 0.0, "break_even_at": None, "reserve": None}


# ---------------------------------------------------------------- Gesamtplan


def _headroom_target_soc(
    config: Dict[str, Any],
    state: Dict[str, Any],
    base_dir: str,
    *,
    t_be: datetime,
) -> Tuple[float, float]:
    """
    s_head und der Tagesüberschuss, der in den Speicher muss.

    Überschuss ist das, was PV + Wasserkraft über Grundlast und alle Geräte
    hinaus liefern – integriert über den ganzen PV-Tag, nicht nur über das
    Deckungsfenster. Nur dieser Anteil kann abgeregelt werden.
    """
    storage = _f(config.get("storage_kwh"))
    max_soc = _f(config.get("max_soc_percent"), 95.0)
    if storage <= 0:
        return max_soc, 0.0

    bias = _params(state).get("drain_device_kw_bias", 1.0)
    total_kw = sum(_device_kw(d, bias) for d in _anchor_first(config))
    day_start = datetime.combine(t_be.date(), datetime.min.time())

    surplus = 0.0
    for start, end in _slot_bounds(day_start, 24.0):
        draw = _net_draw_kwh(state, base_dir, start, end, device_kw=total_kw)
        if draw < 0:
            surplus += -draw
    return max_soc - 100.0 * surplus / storage, surplus


def compute_morning_drain_plan(
    config: Dict[str, Any],
    state: Dict[str, Any],
    live: Dict[str, Any],
    base_dir: str,
    *,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Laufreserve, zulässige Geräte, Drain-Ziel und Frühstart-Zeitpunkt."""
    now = now or datetime.now()
    storage = _f(config.get("storage_kwh"))
    soc = live.get("soc_percent")
    params = _params(state)
    pv_trust = params.get("drain_pv_trust", 0.8)
    bias = params.get("drain_device_kw_bias", 1.0)

    out: Dict[str, Any] = {
        "ok": False,
        # gate_active steuert, ob der Scheduler überhaupt nach g schaltet.
        # Ohne belastbaren Break-even oder solange PV die Last stützt, bleibt
        # die bestehende Logik zuständig – sonst würde ein Prognoseausfall
        # sämtliche Geräte dauerhaft abschalten.
        "gate_active": False,
        "policy": "hold_only",
        "generated_at": now.isoformat(timespec="seconds"),
        "allowed_ips": [],
        "must_empty_kwh": 0.0,
        "accepted_curtail_kwh": 0.0,
        "start_at": None,
        "break_even_at": None,
        "evening_break_even_at": None,
        "g_kwh": None,
        "summary": None,
    }
    if soc is None or storage <= 0:
        out["reason"] = "no_data"
        return out

    soc_f = _f(soc)
    devices = _anchor_first(config)
    total_kw = sum(_device_kw(d, bias) for d in devices)
    if total_kw <= 0:
        out["reason"] = "no_devices"
        return out

    s_floor = floor_soc_percent(config)

    # Ist der Speicher überhaupt die Quelle? Solange die Live-PV den Haushalt
    # trägt, ist das eine Tages- und keine Nachtsituation – dann bleibt die
    # Surplus-/Budgetlogik zuständig, sonst würde ein teilbewölkter Vormittag
    # laufend Geräte abwerfen.
    base_now_kw = _f(_base_kwh(base_dir, now, now + timedelta(hours=1)))
    supply_now_kw = _f(live.get("pv_kw")) + _f(live.get("hydro_kw"))
    battery_is_source = supply_now_kw < max(0.2, base_now_kw)
    out["battery_is_source"] = battery_is_source
    out["base_now_kw"] = round(base_now_kw, 3)

    # Randbedingung: solange PV die Last trägt, fließt keine Speicherenergie –
    # dann entscheidet die Surplus-Logik, nicht g. Ohne diese Regel würde eine
    # kleine Anlage ihre Geräte auch bei voller Sonne nie einschalten.
    evening_be = coverage_end(state, base_dir, device_kw=total_kw, now=now)
    out["evening_break_even_at"] = evening_be.isoformat() if evening_be else None
    if evening_be is not None and now < evening_be:
        out["ok"] = True
        out["policy"] = "keep_on"
        out["allowed_ips"] = [
            str(d.get("ip") or d.get("entity_id") or "") for d in devices
        ]
        out["reason"] = "pv_covers_load"
        out["break_even_at"] = evening_be.isoformat()
        out["summary"] = (
            f"PV trägt die Last bis {evening_be.strftime('%H:%M')} – "
            f"Laufreserve nicht zuständig."
        )
        return out

    reference_be = reference_break_even(
        config, state, base_dir, now=now, pv_scale=pv_trust
    )
    if reference_be is None:
        # Kein verwertbarer Horizont (Prognoseausfall, Schlechtwetter, Last
        # über der PV-Spitze). Gate bleibt aus – die bestehende Budget- und
        # Reservelogik regelt weiter.
        out["ok"] = True
        out["reason"] = "no_break_even"
        out["summary"] = (
            "Kein Break-even im Horizont – Laufreserve nicht anwendbar, "
            "Budget- und Reservelogik übernimmt."
        )
        return out

    out["break_even_at"] = reference_be.isoformat()

    s_head, surplus_kwh = _headroom_target_soc(
        config, state, base_dir, t_be=reference_be
    )
    max_soc = _f(config.get("max_soc_percent"), 95.0)
    s_star = min(max(s_head, s_floor), soc_f)
    must_empty = max(0.0, (soc_f - s_star) / 100.0 * storage)
    accepted_curtail = max(0.0, (s_floor - s_head) / 100.0 * storage)

    allowed = feasible_device_set(
        config,
        state,
        base_dir,
        soc_percent=soc_f,
        now=now,
        t_be=reference_be,
        pv_scale=pv_trust,
    )
    reserve_full = run_reserve_kwh(
        config,
        state,
        base_dir,
        soc_percent=soc_f,
        device_kw=total_kw,
        t_be=reference_be,
        now=now,
        pv_scale=pv_trust,
    )

    out.update(
        {
            "ok": True,
            "allowed_ips": allowed["ips"],
            "allowed_device_kw": allowed["device_kw"],
            "g_kwh": reserve_full["g_kwh"],
            "available_kwh": reserve_full["available_kwh"],
            "need_kwh": reserve_full["need_kwh"],
            "m_risk_kwh": reserve_full["m_risk_kwh"],
            "floor_soc_percent": round(s_floor, 1),
            "headroom_soc_percent": round(s_head, 1),
            "target_soc_percent": round(s_star, 1),
            "current_soc_percent": round(soc_f, 1),
            "max_soc_percent": max_soc,
            "day_surplus_kwh": round(surplus_kwh, 2),
            "must_empty_kwh": round(must_empty, 2),
            "accepted_curtail_kwh": round(accepted_curtail, 2),
            "device_kw": round(total_kw, 3),
            "pv_trust": pv_trust,
        }
    )

    if allowed["ips"] and len(allowed["ips"]) == len(devices):
        out["policy"] = "keep_on"
    elif must_empty > 0:
        out["policy"] = "evening_stop_morning_early"
    else:
        out["policy"] = "hold_only"

    out["gate_active"] = bool(
        battery_is_source and out["policy"] != "keep_on"
    )

    # Ohne Headroom-Bedarf gibt es keinen Grund vorzuziehen – dann auch keine
    # Startzeit melden, sonst suggeriert die UI einen Drain, den es nicht gibt.
    start_at = (
        _first_start(
            config,
            state,
            base_dir,
            soc_percent=soc_f,
            now=now,
            t_be=reference_be,
            device_kw=total_kw,
            pv_scale=pv_trust,
        )
        if must_empty > 0
        else None
    )
    out["start_at"] = start_at.isoformat() if start_at else None

    if allowed["ips"]:
        out["summary"] = (
            f"Laufreserve {reserve_full['g_kwh']:.1f} kWh – "
            f"{len(allowed['ips'])} Gerät(e) freigegeben bis Break-even "
            f"{reference_be.strftime('%d.%m. %H:%M')}."
        )
    elif must_empty > 0:
        when = start_at.strftime("%d.%m. %H:%M") if start_at else "—"
        out["summary"] = (
            f"Laufreserve {reserve_full['g_kwh']:.1f} kWh < 0 – Geräte aus. "
            f"Drain {must_empty:.1f} kWh auf {s_star:.0f} % ab {when} "
            f"(Break-even {reference_be.strftime('%H:%M')})."
        )
    else:
        out["summary"] = (
            f"Kein Headroom-Bedarf (Ziel {s_star:.0f} % ≥ SoC {soc_f:.0f} %) – "
            f"kein Drain, kein Frühstart."
        )
    return out


def _first_start(
    config: Dict[str, Any],
    state: Dict[str, Any],
    base_dir: str,
    *,
    soc_percent: float,
    now: datetime,
    t_be: datetime,
    device_kw: float,
    pv_scale: float,
) -> Optional[datetime]:
    """
    Nulldurchgang von g bei ausgeschalteten Geräten.

    g wächst mit der Geräteleistung, ist also streng monoton – es gibt genau
    einen Durchgang. Bleibt g bis zum Break-even negativ, wird nicht gestartet
    (die Nacht war teurer als prognostiziert).
    """
    storage = _f(config.get("storage_kwh"))
    if storage <= 0:
        return None
    band = deadband_kwh(config, device_kw)
    bias = _params(state).get("drain_base_load_bias", 1.0)
    min_runtime = _f(config.get("min_device_runtime_hours"), 1.0)

    hours = (t_be - now).total_seconds() / 3600.0
    for start, _end in _slot_bounds(now, hours):
        idle_draw = _net_draw_kwh(
            state, base_dir, now, start, device_kw=0.0, base_bias=bias
        )
        soc_at = soc_percent - 100.0 * idle_draw / storage
        reserve = run_reserve_kwh(
            config,
            state,
            base_dir,
            soc_percent=soc_at,
            device_kw=device_kw,
            t_be=t_be,
            now=start,
            pv_scale=pv_scale,
        )
        if reserve["g_kwh"] >= band:
            if (t_be - start).total_seconds() / 3600.0 < min_runtime:
                return None
            return start
    return None


# ---------------------------------------------------------------- Anwendung


def plan_from_state(state: Dict[str, Any]) -> Dict[str, Any]:
    plan = state.get("morning_drain_plan")
    return plan if isinstance(plan, dict) else {}


def device_allowed_on_battery(
    state: Dict[str, Any],
    ip: str,
) -> Tuple[bool, str]:
    """Darf dieses Gerät gerade aus dem Speicher laufen? (g-Gate)"""
    plan = plan_from_state(state)
    if not plan.get("ok") or not plan.get("gate_active"):
        return True, ""
    if str(ip) in set(plan.get("allowed_ips") or []):
        return True, "Laufreserve reicht bis PV-Break-even"
    g = plan.get("g_kwh")
    g_txt = f"{_f(g):.1f} kWh" if g is not None else "—"
    return False, (
        f"Laufreserve {g_txt} – Speicher trägt bis zum PV-Break-even nicht. "
        f"Drain wandert in den Morgen."
    )


def morning_drain_info_messages(state: Dict[str, Any]) -> List[str]:
    plan = plan_from_state(state)
    if not plan.get("ok"):
        return []
    out = []
    if plan.get("summary"):
        out.append(str(plan["summary"]))
    if _f(plan.get("accepted_curtail_kwh")) > 0.1:
        out.append(
            f"Speicher zu klein für die Prognose – "
            f"{_f(plan['accepted_curtail_kwh']):.1f} kWh Abregelung akzeptiert "
            f"(Floor hat Vorrang)."
        )
    return out


# ---------------------------------------------------------------- Lernen


def _learning_alpha(state: Dict[str, Any]) -> float:
    """Ab Tag 1 wirksam. Untere Schranke 0.12, nach einer Woche volles 0.25."""
    from control_algorithm.steering_learning import ensure_steering_learning

    sl = ensure_steering_learning(state)
    n = len(sl.get("history") or [])
    return max(0.12, 0.25 * min(1.0, max(1, n) / 7.0))


def record_morning_drain_tick(
    config: Dict[str, Any],
    state: Dict[str, Any],
    live: Dict[str, Any],
) -> None:
    """Tageszähler für das Abendlernen."""
    plan = plan_from_state(state)
    if not plan.get("ok"):
        return
    soc = live.get("soc_percent")
    if soc is None:
        return

    from control_algorithm.steering_learning import ensure_steering_learning

    sl = ensure_steering_learning(state)
    today = sl["today"]
    soc_f = _f(soc)
    floor = _f(plan.get("floor_soc_percent"), 0.0)

    today["drain_ticks"] = int(today.get("drain_ticks") or 0) + 1
    if plan.get("allowed_ips"):
        today["drain_active_ticks"] = int(today.get("drain_active_ticks") or 0) + 1
    if soc_f < floor:
        today["drain_undershoot_ticks"] = (
            int(today.get("drain_undershoot_ticks") or 0) + 1
        )

    break_even = plan.get("break_even_at")
    if break_even:
        try:
            t_be = datetime.fromisoformat(str(break_even))
        except ValueError:
            t_be = None
        if t_be is not None and abs((datetime.now() - t_be).total_seconds()) <= 600:
            target = _f(plan.get("target_soc_percent"), floor)
            today["drain_arrival_soc_err_sum"] = (
                _f(today.get("drain_arrival_soc_err_sum")) + (soc_f - target)
            )
            today["drain_arrival_samples"] = (
                int(today.get("drain_arrival_samples") or 0) + 1
            )
            if not plan.get("allowed_ips") and _f(plan.get("must_empty_kwh")) > 0.1:
                today["drain_no_start_ticks"] = (
                    int(today.get("drain_no_start_ticks") or 0) + 1
                )

    pv_kw = _f(live.get("pv_kw"))
    if _f(plan.get("must_empty_kwh")) > 0 and soc_f >= _f(
        plan.get("max_soc_percent"), 95.0
    ) - 0.5 and pv_kw > 0.5:
        today["drain_curtail_ticks"] = int(today.get("drain_curtail_ticks") or 0) + 1

    state["steering_learning"] = sl


def run_morning_drain_evening_learning(
    state: Dict[str, Any],
    config: Dict[str, Any],
    analysis: Dict[str, Any],
    params: Dict[str, float],
) -> List[str]:
    """
    Abendkalibrierung. drain_night_risk_frac wird von beiden Seiten
    eingespannt: Floor gerissen hebt ihn, mehrere Tage ohne zustande
    gekommenen Frühstart senken ihn.
    """
    from control_algorithm.steering_learning import (
        _clamp_param,
        _ema,
        ensure_steering_learning,
    )

    sl = ensure_steering_learning(state)
    today = sl.get("today") or {}
    adjustments: List[str] = []
    alpha = _learning_alpha(state)

    undershoot = int(today.get("drain_undershoot_ticks") or 0)
    no_start = int(today.get("drain_no_start_ticks") or 0)
    curtail = int(today.get("drain_curtail_ticks") or 0)
    samples = int(today.get("drain_arrival_samples") or 0)
    arrival_err = _f(today.get("drain_arrival_soc_err_sum")) / samples if samples else 0.0

    def _set(key: str, target: float, note: str) -> None:
        params[key] = _clamp_param(key, _ema(float(params.get(key, 0.0)), target, alpha))
        adjustments.append(note)

    if undershoot >= 1:
        _set(
            "drain_night_risk_frac",
            float(params.get("drain_night_risk_frac", 0.25)) + 0.15,
            f"Morgen-Drain: Floor {undershoot}× unterschritten → Nachtpuffer "
            f"{params.get('drain_night_risk_frac', 0.25):.2f}, Abendstopp früher.",
        )
        _set(
            "drain_base_load_bias",
            float(params.get("drain_base_load_bias", 1.0)) + 0.1,
            "Morgen-Drain: Grundlast war höher als das Profil → Bias angehoben.",
        )
    elif no_start >= 3:
        _set(
            "drain_night_risk_frac",
            float(params.get("drain_night_risk_frac", 0.25)) - 0.08,
            f"Morgen-Drain: {no_start}× kein Frühstart trotz Headroom-Bedarf → "
            f"Nachtpuffer gesenkt.",
        )
    elif samples and arrival_err > 3.0:
        _set(
            "drain_base_load_bias",
            float(params.get("drain_base_load_bias", 1.0)) - 0.05,
            f"Morgen-Drain: {arrival_err:.1f} %-Pkt über Ziel angekommen → "
            f"Grundlast-Bias gesenkt.",
        )

    if curtail >= 2 and undershoot == 0:
        _set(
            "drain_floor_margin_kwh_pct",
            float(params.get("drain_floor_margin_kwh_pct", 2.0)) - 0.5,
            f"Morgen-Drain: {curtail}× Abregelung trotz Drain → Sockel gesenkt.",
        )

    pv_err = _f(analysis.get("pv_forecast_error_pct"))
    if pv_err:
        target = 0.95 if pv_err >= -5 else 0.65
        _set(
            "drain_pv_trust",
            target,
            f"Morgen-Drain: PV-Prognosefehler {pv_err:.0f} % → PV-Vertrauen "
            f"{params.get('drain_pv_trust', 0.8):.2f}.",
        )

    if adjustments:
        sl["last_morning_drain_note"] = adjustments[-1]
        state["steering_learning"] = sl
    return adjustments
