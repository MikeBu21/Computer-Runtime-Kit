#!/usr/bin/env python3
"""
Telegram Speicher-/PCS-Alarm aus batteriescraper (pcs_yx_discovery.json).

Auslöser: mindestens ein überwachter PCS-Status rot/aktiv (wie im Scraper-Terminal).
Recovery: Scraper meldet wieder — alle überwachten Punkte inaktiv.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

HOME = Path.home()
PCS_DISCOVERY_FILE = HOME / "pcs_yx_discovery.json"
PCS_KEYS_FILE = HOME / "battery_scraper_keys.json"
LATCH_FILE = Path(__file__).resolve().parent / "telegram_pcs_latch.json"

# Scraper pollt alle 3 s — Datei älter = Scraper vermutlich aus
SCRAPER_STALE_SECONDS = 45


def _latch_path() -> Path:
    try:
        from ram_cache import backend_ephemeral_path, is_enabled

        if is_enabled():
            return backend_ephemeral_path(
                Path(__file__).resolve().parent,
                "telegram_pcs_latch.json",
            )
    except Exception:
        pass
    return LATCH_FILE


def _config_bool(config: Dict[str, Any], key: str, default: bool = False) -> bool:
    val = config.get(key, default)
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() in ("1", "true", "yes", "on")


def _read_json(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_json(path: Path, data: Dict[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def load_latch() -> Dict[str, Any]:
    return _read_json(_latch_path())


def save_latch(data: Dict[str, Any]) -> None:
    _write_json(_latch_path(), data)


def _load_pcs_key_labels() -> Dict[str, str]:
    data = _read_json(PCS_KEYS_FILE)
    labels: Dict[str, str] = {}
    for entry in data.get("pcs_status_points") or []:
        if isinstance(entry, dict) and entry.get("key"):
            labels[str(entry["key"])] = str(entry.get("label") or entry["key"])
    return labels


def _parse_ts(text: str) -> Optional[datetime]:
    text = str(text or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text[:19], fmt)
        except ValueError:
            continue
    return None


def _discovery_age_seconds(discovery: Dict[str, Any]) -> Optional[float]:
    ts = _parse_ts(str(discovery.get("zeitstempel", "")))
    if ts is None:
        try:
            return max(0.0, datetime.now().timestamp() - PCS_DISCOVERY_FILE.stat().st_mtime)
        except OSError:
            return None
    return max(0.0, (datetime.now() - ts).total_seconds())


def _clear_telegram_pending() -> None:
    disc = _read_json(PCS_DISCOVERY_FILE)
    if not disc:
        return
    disc.pop("telegram_pending", None)
    disc.pop("telegram_pending_labels", None)
    disc.pop("telegram_pending_at", None)
    try:
        PCS_DISCOVERY_FILE.write_text(
            json.dumps(disc, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except Exception:
        pass


def _read_telegram_pending() -> Tuple[bool, List[str]]:
    disc = _read_json(PCS_DISCOVERY_FILE)
    if not disc.get("telegram_pending"):
        return False, []
    labels = disc.get("telegram_pending_labels") or []
    return True, [str(x) for x in labels if x]


def read_scraper_pcs_state() -> Tuple[bool, List[str], Optional[float], str]:
    """
    (scraper_fresh, active_labels, age_seconds, timestamp_str)
    """
    disc = _read_json(PCS_DISCOVERY_FILE)
    age = _discovery_age_seconds(disc)
    fresh = age is not None and age <= SCRAPER_STALE_SECONDS
    ts = str(disc.get("zeitstempel") or "")

    labels_map = _load_pcs_key_labels()
    active_keys = [str(k) for k in (disc.get("aktiv") or []) if k]

    if disc.get("aktiv_labels"):
        active_labels = [str(x) for x in disc["aktiv_labels"] if x]
    else:
        active_labels = [labels_map.get(k, k) for k in active_keys]

    # Nur konfigurierte Überwachungspunkte (die 4 Kreis-Status)
    if labels_map:
        allowed = set(labels_map.keys())
        active_labels = [
            labels_map[k]
            for k in active_keys
            if k in allowed
        ]

    return fresh, active_labels, age, ts


def build_alarm_burst_messages(active_labels: List[str], ts: str) -> List[str]:
    lines = "\n".join(f"🔴 <b>{_esc(lbl)}</b>" for lbl in active_labels)
    detail = lines or "🔴 <b>PCS-Status unbekannt</b>"
    time_line = _esc(ts) if ts else _esc(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    body = (
        "🚨🚨🚨🚨🚨\n"
        "‼️‼️ <b>SPEICHER / PCS ALARM</b> ‼️‼️\n"
        "🚨🚨🚨🚨🚨\n\n"
        f"⏰ {time_line}\n\n"
        f"{detail}\n\n"
        "⚠️ Mindestens ein Status im SEPLOS-Dashboard ist <b>ROT</b>.\n"
        "Bitte Anlage und Speicher <b>sofort</b> vor Ort prüfen!"
    )
    return [body, body, body]


def build_recovery_message(ts: str) -> str:
    time_line = _esc(ts) if ts else ""
    return (
        "✅✅✅\n"
        "<b>SPEICHER / PCS wieder OK</b>\n"
        "✅✅✅\n\n"
        + (f"⏰ {time_line}\n\n" if time_line else "")
        + "Scraper: alle überwachten PCS-Status wieder <b>inaktiv</b> (nicht rot)."
    )


def _esc(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _status_row(active: bool, label: str, detail: str = "") -> str:
    icon = "✅" if active else "❌"
    extra = f" · <i>{_esc(detail)}</i>" if detail else ""
    state = "aktiv" if active else "aus"
    return f"{icon} <b>{_esc(label)}</b>  <i>({state})</i>{extra}"


def build_dashboard_pcs_status_lines(config: Dict[str, Any]) -> List[str]:
    """Zeilen für /status — Überwachung aktiv + aktuelle PCS-Lage vom Scraper."""
    speicher_on = _config_bool(config, "speicher_ausfall_enabled")
    fresh, active, age, ts = read_scraper_pcs_state()
    labels_map = _load_pcs_key_labels()
    disc = _read_json(PCS_DISCOVERY_FILE)
    punkte = disc.get("punkte") if isinstance(disc.get("punkte"), dict) else {}
    latch = load_latch()
    alarm_pending = bool(latch.get("pcs_burst_done"))

    scraper_detail = f"Stand {_esc(ts)}" if ts else ""
    if age is not None and fresh:
        scraper_detail = f"{scraper_detail} · vor {int(age)} s".strip(" ·")

    lines: List[str] = [
        "",
        "🔋 <b>Speicher / PCS (Batteriescraper)</b>",
        _status_row(speicher_on, "Telegram PCS-Alarm"),
        _status_row(fresh, "Batteriescraper verbunden", scraper_detail),
    ]

    if alarm_pending:
        lines.append(
            "⏳ <i>Telegram-Alarm aktiv — wartet auf OK (alle PCS-Punkte grau)</i>"
        )

    if not fresh:
        lines.append("⚠️ <i>Keine frischen Scraper-Daten — scraper.py läuft?</i>")
    elif active:
        red_list = ", ".join(_esc(l) for l in active)
        lines.append(f"🚨 <b>Lage Speicher:</b> <b>ALARM</b> — {_esc(red_list)}")
    else:
        lines.append("✅ <b>Lage Speicher:</b> alle überwachten PCS-Status OK")

    if labels_map:
        lines.append("<i>PCS-Detail:</i>")
        aktiv_set = set(disc.get("aktiv") or [])
        for key in sorted(labels_map.keys()):
            label = labels_map[key]
            val = punkte.get(key, "—")
            is_red = key in aktiv_set
            icon = "🔴" if is_red else "🟢"
            state = "ROT" if is_red else "OK"
            lines.append(
                f"{icon} <b>{_esc(label)}</b>  <code>{_esc(val)}</code>  <i>({state})</i>"
            )

    lines.append("")
    return lines


def evaluate_pcs_telegram_alarm(
    config: Dict[str, Any],
    latch: Optional[Dict[str, Any]] = None,
) -> Tuple[Optional[List[str]], Optional[str], bool]:
    """
    Returns (burst_messages_html, recovery_message_html, use_html).
    burst_messages: 3 Telegram texts when new alarm.
    recovery_message: one OK message when cleared.
    """
    if not _config_bool(config, "speicher_ausfall_enabled"):
        lat = latch if latch is not None else load_latch()
        lat["pcs_burst_done"] = False
        lat["pcs_last_active"] = []
        save_latch(lat)
        return None, None, True

    lat = latch if latch is not None else load_latch()
    fresh, active, age, ts = read_scraper_pcs_state()
    pending, pending_labels = _read_telegram_pending()
    notify_labels = pending_labels if pending_labels else active

    if lat.get("pcs_burst_done"):
        if fresh and not active:
            lat["pcs_burst_done"] = False
            lat["pcs_burst_at"] = None
            lat["pcs_last_active"] = []
            save_latch(lat)
            _clear_telegram_pending()
            return None, build_recovery_message(ts), True
        # Neuer [PCS-ALARM] im Scraper trotz noch rot → erneut 3× Telegram
        if pending and fresh and notify_labels:
            lat["pcs_burst_done"] = True
            lat["pcs_burst_at"] = ts or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            lat["pcs_last_active"] = list(notify_labels)
            save_latch(lat)
            _clear_telegram_pending()
            return build_alarm_burst_messages(notify_labels, ts), None, True
        return None, None, True

    if not fresh:
        return None, None, True

    if not active and not pending:
        lat["pcs_last_active"] = []
        save_latch(lat)
        return None, None, True

    if not notify_labels:
        return None, None, True

    lat["pcs_burst_done"] = True
    lat["pcs_burst_at"] = ts or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lat["pcs_last_active"] = list(notify_labels)
    save_latch(lat)
    _clear_telegram_pending()

    return build_alarm_burst_messages(notify_labels, ts), None, True
